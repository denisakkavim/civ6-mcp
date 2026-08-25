"""High-level game state API with server-side narration.

Wraps GameConnection + lua into typed async methods that return
both structured data and human-readable narrated text. Has ZERO MCP
dependency — enabling multi-agent architectures where specialist servers
import the same GameState class but expose different tool subsets.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

from civ_mcp import ids
from civ_mcp import lua as lq
from civ_mcp.connection import GameConnection
from civ_mcp.narrate import (
    narrate_combat_estimate,
    narrate_move_discoveries,
    narrate_settle_candidates,
    narrate_test_trade,
)

log = logging.getLogger(__name__)


class GameState:
    """High-level async API for Civ 6 game state + actions."""

    def __init__(self, connection: GameConnection):
        self.conn = connection
        self._last_snapshot: lq.TurnSnapshot | None = None
        self._game_identity: tuple[str, int] | None = None  # (civ_type, seed)
        # Tiles the player has already revealed, for post-move discovery
        # feedback. None until seeded lazily on the first move; reset on
        # game change and on save load (an older save has revealed less).
        self._revealed: set[tuple[int, int]] | None = None
        self._pending_end_turn: bool = False  # ACTION_ENDTURN already in flight
        self._pending_end_turn_from: int | None = (
            None  # turn number when ACTION_ENDTURN was sent
        )
        self._high_water_turn: int = 0  # highest turn seen (for regression detection)
        self._local_player_id: int = 0  # human player (always 0 in single-player)
        self._hang_retry_active: bool = False  # guard against recursive hang recovery
        self._last_game_over: lq.GameOverStatus | None = (
            None  # captured by execute_end_turn for server.py
        )
        # (ts, turn, save_name) for each successful save load — used to detect
        # save scumming in _check_save_scumming(). Bounded to last 50 entries.
        self._save_load_history: list[tuple[float, int, str]] = []
        self._run_aborted: bool = False  # set when save scumming threshold is exceeded
        # Per-turn advisor call budget — prevents compulsive advisor loops
        # (e.g. Gemini Pro's 1,567 get_wonder_advisor calls in a single turn).
        # Reset in execute_end_turn on successful turn advance.
        self._advisor_calls_this_turn: int = 0
        # One-shot warning from the most recent advisor call, consumed and
        # cleared by the server wrapper.
        self._advisor_budget_warning: str | None = None

    async def get_game_identity(self) -> tuple[str, int]:
        """Return (civ_type_lower, random_seed) for the current game.

        Always queries the game so we detect new-game loads.  When the
        identity changes, all per-game cached state is reset.
        """
        code = (
            "local me = Game.GetLocalPlayer() "
            "local cfg = PlayerConfigurations[me] "
            'print("GAMESEED|" .. cfg:GetCivilizationTypeName() '
            '.. "|" .. tostring(GameConfiguration.GetValue("GAME_SYNC_RANDOM_SEED"))) '
            'print("---END---")'
        )
        lines = await self.conn.execute_write(code)
        for line in lines:
            if line.startswith("GAMESEED|"):
                parts = line.split("|")
                civ = parts[1].replace("CIVILIZATION_", "").lower()
                seed = int(parts[2])
                new_id = (civ, seed)
                if self._game_identity is not None and new_id != self._game_identity:
                    log.info("Game changed: %s → %s", self._game_identity, new_id)
                    self._last_snapshot = None
                    self._revealed = None
                    self._last_game_over = None
                    self._save_load_history = []
                    self._run_aborted = False
                    self._advisor_calls_this_turn = 0
                    self._advisor_budget_warning = None
                self._game_identity = new_id
                return self._game_identity
        return ("unknown", 0)

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    async def get_game_overview(self) -> lq.GameOverview:
        # InGame context needed for GetFavor() (nil in GameCore)
        lines = await self.conn.execute_write(lq.build_overview_query())
        ov = lq.parse_overview_response(lines)
        # Bootstrap: capture baseline snapshot for first end_turn diff
        if self._last_snapshot is None:
            try:
                self._last_snapshot = await self._take_snapshot(ov)
            except Exception:
                log.debug("Failed to bootstrap snapshot", exc_info=True)
        return ov

    async def check_game_over(self) -> lq.GameOverStatus | None:
        """Check if the game has ended (victory/defeat screen showing).

        Tries InGame context first (full detection with UI checks).
        Falls back to GameCore context (read-only, survives defeat screen)
        when InGame fails — this catches victories that freeze the InGame UI.
        """
        try:
            lines = await self.conn.execute_write(lq.build_gameover_check())
            return lq.parse_gameover_response(lines)
        except Exception:
            log.debug("Game-over check failed in InGame, trying GameCore")
        # Fallback: GameCore-only check (survives defeat screen)
        try:
            lines = await self.conn.execute_read(lq.build_gameover_check_gamecore())
            return lq.parse_gameover_response(lines)
        except Exception:
            log.debug("Game-over check failed in GameCore too", exc_info=True)
            return None

    async def get_units(self) -> list[lq.UnitInfo]:
        lines = await self.conn.execute_write(lq.build_units_query())
        return lq.parse_units_response(lines)

    async def get_builder_tasks(
        self,
    ) -> tuple[list[lq.BuilderTask], list[lq.BuilderInfo]]:
        lines = await self.conn.execute_write(lq.build_builder_tasks_query())
        return lq.parse_builder_tasks(lines)

    async def get_spies(self) -> list[lq.SpyInfo]:
        lines = await self.conn.execute_write(lq.build_get_spies_query())
        return lq.parse_spies_response(lines)

    async def spy_travel(self, unit_index: int, target_x: int, target_y: int) -> str:
        lua = lq.build_spy_travel(unit_index, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def spy_mission(
        self, unit_index: int, mission_type: str, target_x: int, target_y: int
    ) -> str:
        lua = lq.build_spy_mission(unit_index, mission_type, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def get_threat_scan(self) -> list[lq.ThreatInfo]:
        lines = await self.conn.execute_read(lq.build_threat_scan_query())
        return lq.parse_threat_scan_response(lines)

    async def get_pathing_estimate(
        self, unit_index: int, target_x: int, target_y: int
    ) -> lq.PathingEstimate:
        lines = await self.conn.execute_write(
            lq.build_pathing_estimate_query(unit_index, target_x, target_y)
        )
        return lq.parse_pathing_estimate(lines)

    async def get_victory_progress(self) -> lq.VictoryProgress:
        lines = await self.conn.execute_write(lq.build_victory_progress_query())
        return lq.parse_victory_progress_response(lines)

    async def get_cities(self) -> tuple[list[lq.CityInfo], list[str]]:
        lines = await self.conn.execute_write(lq.build_cities_query())
        return lq.parse_cities_response(lines)

    async def get_map_area(
        self, center_x: int, center_y: int, radius: int = 2
    ) -> list[lq.TileInfo]:
        lines = await self.conn.execute_read(
            lq.build_map_area_query(center_x, center_y, radius)
        )
        return lq.parse_map_response(lines)

    async def get_strategic_map(self) -> lq.StrategicMapData:
        lines = await self.conn.execute_read(lq.build_strategic_map_query())
        return lq.parse_strategic_map_response(lines)

    async def get_diplomacy(self) -> list[lq.CivInfo]:
        # Uses InGame context for GetDiplomaticAI access
        lines = await self.conn.execute_write(lq.build_diplomacy_query())
        return lq.parse_diplomacy_response(lines)

    async def get_tech_civics(self) -> lq.TechCivicStatus:
        lines = await self.conn.execute_read(lq.build_tech_civics_query())
        return lq.parse_tech_civics_response(lines)

    async def get_empire_resources(
        self,
    ) -> tuple[
        list[lq.ResourceStockpile],
        list[lq.OwnedResource],
        list[lq.NearbyResource],
        dict[str, int],
    ]:
        # InGame context needed for GetResourceStockpileCap etc.
        lines = await self.conn.execute_write(lq.build_empire_resources_query())
        return lq.parse_empire_resources_response(lines)

    # ------------------------------------------------------------------
    # Action methods (run in InGame context for UnitManager access)
    # ------------------------------------------------------------------

    async def _seed_revealed_tiles(self) -> None:
        """Populate the revealed-tile set. No-op once seeded.

        Called before the first move rather than at session start so that
        the very first move still reports what it uncovers.
        """
        if self._revealed is not None:
            return
        lines = await self.conn.execute_read(lq.build_revealed_tiles_seed_query())
        self._revealed = lq.parse_revealed_tiles_seed(lines)
        log.info("Seeded revealed-tile set with %d tiles", len(self._revealed))

    async def resolve_city_position(self, city_id: int) -> tuple[int, int, str] | str:
        """Turn a composite city id into ``(x, y, name)``, or an error string.

        Keeps both halves of the id. Own-city tools drop the owner because
        their Lua assumes the local player, but a foreign city is only
        findable through the player that holds it, so this must not reduce the
        id to its low bits.
        """
        owner = ids.owner_of(city_id)
        local_id = ids.local_of(city_id)
        lines = await self.conn.execute_read(
            lq.build_city_position_query(owner, local_id)
        )
        position = lq.parse_city_position(lines)
        if position is None:
            return (
                f"Error: CITY_NOT_FOUND|No city {city_id} (player {owner},"
                f" city {local_id}). Ids change when a city is captured and are"
                f" recycled after razing — re-read get_cities or get_diplomacy."
            )
        return position

    async def read_unit_position(self, unit_index: int) -> tuple[int, int, int] | None:
        """Return ``(x, y, moves_remaining)``, or None if the unit is gone.

        ``moves_remaining`` is 0 when the game gave no value, which is the
        conservative reading: a caller uses it to decide whether the unit can
        still act this turn.
        """
        lines = await self.conn.execute_read(lq.build_unit_position_query(unit_index))
        for line in lines:
            if not line.startswith("POS|") or "GONE" in line:
                continue
            parts = line.split("|")
            if len(parts) < 3:
                continue
            moves = 0
            if len(parts) > 3 and parts[3].strip():
                moves = int(float(parts[3]))
            return int(parts[1]), int(parts[2]), moves
        return None

    async def read_unit_kind(self, unit_index: int) -> tuple[str, str, int, int] | None:
        """Return the unit's type, its formation class and its tile, or None if gone.

        A spy that is travelling reports ``x = -9999``: the engine has taken it
        off the map until it arrives. Callers must treat that as "not anywhere
        yet" rather than as a tile.
        """
        lines = await self.conn.execute_read(lq.build_unit_kind_query(unit_index))
        for line in lines:
            if not line.startswith("KIND|") or "GONE" in line:
                continue
            parts = line.split("|")
            if len(parts) < 5:
                continue
            return parts[1], parts[2], int(parts[3]), int(parts[4])
        return None

    async def move_then_act(
        self,
        unit_index: int,
        verb: str,
        target_x: int,
        target_y: int,
        act: Callable[[], Awaitable[str]],
    ) -> str:
        """Move the unit to (target_x, target_y) if needed, then run ``act``.

        Collapses the two-turn "move there, then improve it next turn" chain
        into one call. There are three outcomes, and they are deliberately
        distinct, because the agent's next move differs in each:

        - acted: the action's own result, noting the move.
        - did not arrive: ``MOVED_PARTIAL`` — re-issue the same call next turn.
        - arrived with no movement left and the action refused:
          ``ARRIVED_WAITING`` — re-issue without moving. Builder verbs do
          require movement, so this case is real; it was measured on a live
          game rather than assumed.

        The action never runs unless the unit is standing on the target tile,
        so a result never claims an action happened when only the move did.
        """
        before = await self.read_unit_position(unit_index)
        if before is None:
            return f"Error: UNIT_GONE|Unit {unit_index} no longer exists."

        if (before[0], before[1]) == (target_x, target_y):
            return await act()

        move_result = await self.move_unit(unit_index, target_x, target_y)

        after = await self.read_unit_position(unit_index)
        if after is None:
            return f"Error: UNIT_GONE|Unit {unit_index} was lost while moving."
        now_x, now_y, moves_left = after

        if (now_x, now_y) != (target_x, target_y):
            return (
                f"MOVED_PARTIAL|at=({now_x},{now_y})"
                f"|target=({target_x},{target_y})"
                f"|remaining={moves_left}"
                f"|Re-issue {verb} with the same target next turn."
                f" Move detail: {move_result}"
            )

        result = await act()

        if result.startswith("Error:") and moves_left == 0:
            return (
                f"ARRIVED_WAITING|at=({target_x},{target_y})"
                f"|remaining=0"
                f"|{verb} needs movement and the move used all of it."
                f" Re-issue {verb} next turn without moving."
                f" Game said: {result}"
            )

        return f"{result} (moved to {target_x},{target_y})"

    async def move_unit(self, unit_index: int, target_x: int, target_y: int) -> str:
        # Seed before moving so the first move of a session reports
        # discoveries. Warn loudly on failure — a silent failure here once
        # disabled this feature for months.
        try:
            await self._seed_revealed_tiles()
        except Exception:
            log.warning("Failed to seed revealed tiles", exc_info=True)
        lua = lq.build_move_unit(unit_index, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        # Post-move: read actual position from GameCore (move is async in InGame)
        if result.startswith("MOVING_TO") or result.startswith("CAPTURE_MOVE"):
            try:
                pos_lines = await self.conn.execute_read(
                    lq.build_unit_position_query(
                        unit_index,
                        move_target_x=target_x,
                        move_target_y=target_y,
                    )
                )
                for line in pos_lines:
                    if line.startswith("POS|") and "GONE" not in line:
                        parts = line.split("|")
                        now_x, now_y = int(parts[1]), int(parts[2])
                        result += f"|now_at:{now_x},{now_y}"
                        from_match = re.search(r"\|from:(\d+),(\d+)", result)
                        if from_match:
                            from_x = int(from_match.group(1))
                            from_y = int(from_match.group(2))
                            if now_x == from_x and now_y == from_y:
                                reason = lq.parse_blocked_diagnostic(pos_lines)
                                result += f"|BLOCKED ({reason})"
                            else:
                                dx = now_x - from_x
                                dy = (
                                    now_y - from_y
                                )  # positive dy = south (higher Y = south in Civ 6)
                                result += f"|(moved dx:{dx:+d} dy:{dy:+d})"
                                tgt_match = re.search(
                                    r"(?:MOVING_TO|CAPTURE_MOVE)\|(\d+),(\d+)", result
                                )
                                if tgt_match:
                                    tx, ty = (
                                        int(tgt_match.group(1)),
                                        int(tgt_match.group(2)),
                                    )
                                    if (now_x, now_y) != (tx, ty):
                                        result += "|STOPPED_MID_PATH (moves exhausted)"
                        break
            except Exception:
                pass
        # Post-move: diff visibility against the revealed set for discovery
        # feedback. Skipped when the unit did not actually move.
        if "|BLOCKED" not in result and self._revealed is not None:
            try:
                now_match = re.search(r"now_at:(\d+),(\d+)", result)
                if now_match:
                    vis_x, vis_y = int(now_match.group(1)), int(now_match.group(2))
                    vis_lines = await self.conn.execute_read(
                        lq.build_post_move_visibility_query(vis_x, vis_y)
                    )
                    vis_tiles = lq.parse_post_move_visibility(vis_lines)
                    newly_revealed = {(x, y) for x, y, _ in vis_tiles} - self._revealed
                    if newly_revealed:
                        self._revealed |= newly_revealed
                        new_tile_data = [
                            (x, y, m)
                            for x, y, m in vis_tiles
                            if (x, y) in newly_revealed
                        ]
                        discovery_text = narrate_move_discoveries(
                            new_tile_data, len(newly_revealed)
                        )
                        if discovery_text:
                            result += "\n" + discovery_text
            except Exception:
                log.warning("Post-move visibility diff failed", exc_info=True)
        return result

    async def attack_unit(self, unit_index: int, target_x: int, target_y: int) -> str:
        # Pre-attack: run combat estimator
        estimate_str = ""
        est: lq.CombatEstimate | None = None
        try:
            est_lua = lq.build_combat_estimate_query(unit_index, target_x, target_y)
            est_lines = await self.conn.execute_write(est_lua)
            est = lq.parse_combat_estimate(est_lines, 0, 0)
            if est:
                estimate_str = narrate_combat_estimate(est) + "\n"
        except Exception as e:
            log.debug("Combat estimate failed: %s", e)
        lua = lq.build_attack_unit(unit_index, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        # Combat followup: the game engine processes combat asynchronously
        # after RequestOperation.  Lua state within the same turn frame
        # does NOT reflect post-combat HP regardless of how long we wait.
        # Strategy: use the combat estimate as authoritative damage source
        # and only query the tile to detect if the target was eliminated.
        is_melee = result.startswith("MELEE_ATTACK")
        is_air = result.startswith("AIR_ATTACK")
        if result.startswith("RANGE_ATTACK") or is_melee or is_air:
            pre_hp = _extract_pre_hp(result)
            est_dmg = est.est_damage_to_defender if est else None
            local_id = self._local_player_id

            # Brief delay then check if target still exists on the tile
            await asyncio.sleep(0.3 if not is_melee else 0.5)
            followup: list[str] = []
            try:
                followup = await self.conn.execute_write(
                    lq.build_attack_followup_query(target_x, target_y)
                )
            except Exception as e:
                log.debug("Attack followup read failed: %s", e)

            try:
                followup_str = _format_attack_followup(followup, local_id)
                city_def = _extract_city_defense(followup)

                # Check if target was eliminated (no enemy units on tile)
                enemy_units = [
                    l
                    for l in followup
                    if l.startswith("UNIT|") and f"owner:{local_id}" not in l
                ]
                eliminated = not enemy_units

                # Build damage report from estimate (authoritative) or followup
                post_hp = _extract_post_hp(followup, local_id)
                damage_info = ""
                if eliminated and pre_hp is not None:
                    damage_info = f"|damage dealt:{pre_hp} (killed)"
                    followup_str = "Target eliminated"
                elif pre_hp is not None and post_hp is not None and post_hp < pre_hp:
                    # Followup reflects real change (can happen for city attacks)
                    damage_info = f"|damage dealt:{pre_hp - post_hp}"
                elif est_dmg and est_dmg > 0 and pre_hp is not None:
                    # Followup stale — use estimate as best available
                    capped_dmg = min(est_dmg, pre_hp)
                    est_post = pre_hp - capped_dmg
                    damage_info = f"|est damage dealt:~{capped_dmg}"
                    followup_str = (
                        f"~{est_post}/{pre_hp} (estimate — verify with get_units)"
                    )

                if city_def:
                    w_hp, w_max, g_hp, g_max = city_def
                    if w_max > 0:
                        damage_info += (
                            f"|city walls: {w_hp}/{w_max}, garrison: {g_hp}/{g_max}"
                        )

                result += damage_info + "\n  Post-combat: " + followup_str
            except Exception as e:
                log.debug("Attack followup formatting failed: %s", e)
        return estimate_str + result

    async def attack_from_city(self, city_id: int, target_x: int, target_y: int) -> str:
        lua = lq.build_attack_from_city(city_id, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if result.startswith("CITY_RANGE_ATTACK"):
            pre_hp = _extract_pre_hp(result)
            await asyncio.sleep(0.3)
            followup: list[str] = []
            try:
                followup = await self.conn.execute_write(
                    lq.build_attack_followup_query(target_x, target_y)
                )
            except Exception:
                followup = []
            try:
                followup_str = _format_attack_followup(followup)
                post_hp = _extract_post_hp(followup)
                damage_info = ""
                if pre_hp is not None and post_hp is not None and post_hp < pre_hp:
                    damage_info = f"|damage dealt:{pre_hp - post_hp}"
                elif not any(l.startswith("UNIT|") for l in followup):
                    if pre_hp is not None:
                        damage_info = f"|damage dealt:{pre_hp} (killed)"
                    followup_str = "Target eliminated"

                result += damage_info + "\n  Post-combat: " + followup_str
            except Exception as e:
                log.debug("City attack followup failed: %s", e)
        return result

    async def resolve_city_capture(self, action: str) -> str:
        lua = lq.build_resolve_city_capture(action)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def found_city(self, unit_index: int) -> str:
        lua = lq.build_found_city(unit_index)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)

        if result.startswith("FOUNDED|"):
            # Extract coordinates from "FOUNDED|x,y"
            parts = result.split("|")[1].split(",")
            x, y = int(parts[0]), int(parts[1])
            # Verify city was actually created (RequestOperation is async)
            verify_lua = lq.build_verify_city_at(x, y)
            verify_lines = await self.conn.execute_read(verify_lua)
            verified = lq.parse_verify_city_at(verify_lines)
            if not verified:
                # Retry once — popup may have blocked the async operation
                try:
                    await self.dismiss_popup()
                    lines = await self.conn.execute_write(lua)
                    retry_result = _action_result(lines)
                    if retry_result.startswith("FOUNDED|"):
                        verify_lines = await self.conn.execute_read(verify_lua)
                        if lq.parse_verify_city_at(verify_lines):
                            result = retry_result
                            verified = True
                except Exception:
                    log.debug(
                        "found_city retry after popup dismiss failed", exc_info=True
                    )
                if not verified:
                    result = (
                        f"Error: FOUND_FAILED|Founding at {x},{y} was requested but "
                        "city did not appear despite popup dismissal."
                    )

        # On settle failure, run the settle advisor to suggest alternatives
        if result.startswith("Error: CANNOT_FOUND") or result.startswith(
            "Error: FOUND_FAILED"
        ):
            try:
                advisor_result = await self.get_settle_advisor(unit_index)
                result += "\n\n" + advisor_result
            except Exception as e:
                log.debug("Settle advisor failed: %s", e)
        return result

    async def get_settle_advisor(self, unit_index: int) -> str:
        lua = lq.build_settle_advisor_query(unit_index)
        lines = await self.conn.execute_read(lua)
        candidates = lq.parse_settle_advisor_response(lines)
        if candidates:
            await self._add_turns_to_reach(unit_index, candidates)
            return narrate_settle_candidates(candidates)
        # Auto-fallback to global scan when no local candidates
        try:
            global_candidates = await self.get_global_settle_scan()
            if global_candidates:
                nearest = global_candidates[:5]
                await self._add_turns_to_reach(unit_index, nearest)
                header = "No valid settle locations within 5 tiles. Best sites on revealed map:\n"
                return header + narrate_settle_candidates(nearest)
        except Exception:
            log.debug("Global settle fallback failed", exc_info=True)
        return "No valid settle locations found within 5 tiles or on revealed map."

    async def _add_turns_to_reach(
        self, unit_index: int, candidates: list[lq.SettleCandidate]
    ) -> None:
        """Fill in turns_to_reach for each candidate, in place.

        A ranking of settle sites is nearly useless without it: the agent
        otherwise has to ask for a pathing estimate per candidate before it can
        compare them. One query per candidate, and a failure leaves the field
        as None rather than failing the whole read.
        """
        for candidate in candidates:
            try:
                estimate = await self.get_pathing_estimate(
                    unit_index, candidate.x, candidate.y
                )
                candidate.turns_to_reach = estimate.turns
            except Exception:
                log.debug(
                    "Pathing estimate failed for (%d,%d)",
                    candidate.x,
                    candidate.y,
                    exc_info=True,
                )

    async def get_global_settle_scan(self) -> list[lq.SettleCandidate]:
        lua = lq.build_global_settle_scan()
        lines = await self.conn.execute_read(lua)
        return lq.parse_settle_advisor_response(lines)

    async def fortify_unit(self, unit_index: int) -> str:
        lua = lq.build_fortify_unit(unit_index)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if result.startswith("SLEEPING"):
            return "Unit is sleeping (this unit type cannot fortify)"
        return result

    async def skip_unit(self, unit_index: int) -> str:
        lua = lq.build_skip_unit(unit_index)
        lines = await self.conn.execute_read(lua)
        return _action_result(lines)

    async def skip_remaining_units(self) -> str:
        """Settle every unit that still has moves, in two passes.

        The first pass fortifies combat units, or heals the damaged ones,
        because leaving a soldier idle and leaving it dug in are not the same
        thing. The second finishes the moves of whatever the first could not
        place. Each pass is one Lua loop over the roster rather than one call
        per unit, and both filter to units that still have movement.
        """
        # First try to fortify/heal combat units (InGame context)
        fortify_result = ""
        try:
            lua_fort = lq.build_fortify_remaining_units()
            fort_lines = await self.conn.execute_write(lua_fort)
            fortify_result = _action_result(fort_lines)
        except Exception as e:
            log.debug("Fortify remaining failed: %s", e)
        # Then skip anything still with moves (GameCore context)
        lua = lq.build_skip_remaining_units()
        lines = await self.conn.execute_read(lua)
        skip_result = _action_result(lines)
        if fortify_result and not fortify_result.startswith("Error"):
            return f"{fortify_result}\n{skip_result}"
        return skip_result

    async def automate_explore(self, unit_index: int) -> str:
        lua = lq.build_automate_explore(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def heal_unit(self, unit_index: int) -> str:
        lua = lq.build_heal_unit(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def alert_unit(self, unit_index: int) -> str:
        lua = lq.build_alert_unit(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def sleep_unit(self, unit_index: int) -> str:
        lua = lq.build_sleep_unit(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def delete_unit(self, unit_index: int) -> str:
        lua = lq.build_delete_unit(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def improve_tile(self, unit_index: int, improvement_name: str) -> str:
        lua = lq.build_improve_tile(unit_index, improvement_name)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def remove_feature(self, unit_index: int) -> str:
        lua = lq.build_remove_feature(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def repair_improvement(self, unit_index: int) -> str:
        lua = lq.build_repair_improvement(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def remove_improvement(self, unit_index: int) -> str:
        lua = lq.build_remove_improvement(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def sacrifice_builder_charges(self, unit_index: int) -> str:
        lua = lq.build_sacrifice_builder_charges(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def build_route(self, unit_index: int) -> str:
        lua = lq.build_build_route(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def set_city_production(
        self,
        city_id: int,
        item_type: str,
        item_name: str,
        target_x: int | None = None,
        target_y: int | None = None,
    ) -> str:
        itype = item_type.upper()

        # No tile given: ask the advisor and build on its top-ranked pick, so
        # placing a district is one call rather than advisor-then-copy-coords.
        # A district always needs a tile, so resolve it before the write.
        placement_note = ""
        if itype == "DISTRICT" and target_x is None:
            chosen = await self._auto_place_district(city_id, item_name)
            if isinstance(chosen, str):
                return chosen
            target_x, target_y, placement_note = chosen

        lua = lq.build_produce_item(city_id, item_type, item_name, target_x, target_y)
        lines = await self.conn.execute_write(lua)

        # A wonder needs a tile too, but only the game database knows which
        # buildings are wonders, so the check is the game's. It bails before
        # RequestOperation, so nothing has been mutated and a retry is safe.
        # Ordinary buildings therefore pay nothing for this path.
        if itype == "BUILDING" and target_x is None and _is_missing_coords(lines):
            chosen = await self._auto_place_wonder(city_id, item_name)
            if isinstance(chosen, str):
                return chosen
            target_x, target_y, placement_note = chosen
            lua = lq.build_produce_item(
                city_id, item_type, item_name, target_x, target_y
            )
            lines = await self.conn.execute_write(lua)

        result = _action_result(lines)

        # If CanStartOperation failed but CanProduce passed, verify via readback
        if any("MAYBE:" in l for l in lines):
            try:
                verify_lines = await self.conn.execute_read(
                    lq.build_verify_production(city_id, item_name)
                )
                if any("CONFIRMED" in l for l in verify_lines):
                    turns = ""
                    for vl in verify_lines:
                        if vl.startswith("CONFIRMED|"):
                            turns = vl.split("|", 1)[1]
                    return (
                        f"PRODUCING|{item_name}|{turns}"
                        f" (bypassed stale CanStartOperation){placement_note}"
                    )
                else:
                    hint = ""
                    if itype == "DISTRICT":
                        hint = f" Tried ({target_x},{target_y})."
                        try:
                            placements = await self.get_district_advisor(
                                city_id, item_name
                            )
                            if isinstance(placements, list) and placements:
                                alts = ", ".join(
                                    f"({p.x},{p.y}) Adj +{p.total_adjacency}"
                                    for p in placements[:5]
                                )
                                hint += f" Valid tiles: {alts}."
                        except Exception:
                            pass
                        hint += " Use get_district_sites for details."
                    elif itype == "BUILDING":
                        # Check if Lua reported pillaged districts
                        pillaged_dists = ""
                        for ml in lines:
                            if "PILLAGED:" in ml:
                                pillaged_dists = ml.split("PILLAGED:", 1)[1]
                                break
                        if pillaged_dists:
                            hint = (
                                f" Prerequisite district is pillaged:"
                                f" {pillaged_dists}. Repair it first via"
                                " set_city_production(city_id, 'DISTRICT_NAME',"
                                " x, y) — use get_cities to find district"
                                " coordinates."
                            )
                        else:
                            bld_info = item_name.replace("BUILDING_", "")
                            hint = (
                                f" Hint: {bld_info} may require a completed"
                                " district or prerequisite building."
                            )
                    return f"Error: CANNOT_START|{item_name} cannot start.{hint}"
            except Exception:
                log.debug("Production readback failed", exc_info=True)
                return f"Error: CANNOT_START|{item_name} (readback failed)"

        # OK-path verification. RequestOperation is fire-and-forget; even
        # when CanStartOperation returned true it can silently no-op if the
        # queue is in a degenerate state. Round-trip read to confirm.
        if result.startswith("PRODUCING|"):
            try:
                verify_lines = await self.conn.execute_read(
                    lq.build_verify_production(city_id, item_name)
                )
                if any("CONFIRMED" in vl for vl in verify_lines):
                    return result + placement_note
                not_set = next(
                    (vl for vl in verify_lines if vl.startswith("NOT_SET|")),
                    "NOT_SET|unknown",
                )
                return (
                    f"Error: SILENT_FAILURE|{item_name} appeared to set but "
                    f"the game engine did not persist it ({not_set}). Retry "
                    f"the same call, or use purchase_item to force-commit "
                    f"with gold/faith."
                )
            except Exception:
                log.debug("OK-path production verify failed", exc_info=True)

        # The note names a tile the server chose, so it belongs only on a
        # result that actually started building on it.
        if result.startswith("PRODUCING|"):
            return result + placement_note
        return result

    async def _auto_place_district(
        self, city_id: int, district_type: str
    ) -> tuple[int, int, str] | str:
        """Pick the top-adjacency tile for a district.

        Returns ``(x, y, note)`` or an error string for the caller to return
        verbatim. The advisor call is exempt from the per-turn budget.
        """
        placements = await self.get_district_advisor(
            city_id, district_type, enforce_budget=False
        )
        if isinstance(placements, str):
            return placements
        if not placements:
            return (
                f"Error: NO_PLACEMENT|No valid tile for {district_type} in this"
                f" city. Use get_district_sites(city_id, '{district_type}') to"
                f" see why, or pass target_x/target_y yourself."
            )

        best = placements[0]
        adjacency_parts = []
        for yield_type, bonus in best.adjacency.items():
            adjacency_parts.append(f"{bonus} {yield_type}")
        adjacency_text = ", ".join(adjacency_parts) if adjacency_parts else "none"
        note = (
            f" | auto-placed at ({best.x},{best.y})"
            f" Adj +{best.total_adjacency} ({adjacency_text})"
            f" — {len(placements)} tiles considered."
            f" Pass target_x/target_y to choose a different tile."
        )
        return best.x, best.y, note

    async def _auto_place_wonder(
        self, city_id: int, wonder_type: str
    ) -> tuple[int, int, str] | str:
        """Pick the lowest-displacement tile for a wonder.

        Returns ``(x, y, note)`` or an error string for the caller to return
        verbatim. The advisor call is exempt from the per-turn budget.
        """
        placements = await self.get_wonder_advisor(
            city_id, wonder_type, enforce_budget=False
        )
        if isinstance(placements, str):
            return placements
        if not placements:
            return (
                f"Error: NO_PLACEMENT|No valid tile for {wonder_type} in this"
                f" city. Use get_wonder_sites(city_id, '{wonder_type}') to see"
                f" why, or pass target_x/target_y yourself."
            )

        best = placements[0]
        note = (
            f" | auto-placed at ({best.x},{best.y})"
            f" displacement {best.displacement_score}"
            f" — {len(placements)} tiles considered."
            f" A wonder consumes the tile permanently; pass target_x/target_y"
            f" if you are saving it for a district."
        )
        return best.x, best.y, note

    async def purchase_item(
        self,
        city_id: int,
        item_type: str,
        item_name: str,
        yield_type: str = "YIELD_GOLD",
    ) -> str:
        lua = lq.build_purchase_item(city_id, item_type, item_name, yield_type)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def list_city_production(self, city_id: int) -> list[lq.ProductionOption]:
        lua = lq.build_city_production_query(city_id)
        # Must use InGame context — bq:CanProduce() throws "Not Implemented" in GameCore
        lines = await self.conn.execute_write(lua)
        return lq.parse_city_production_response(lines)

    async def set_research(self, tech_name: str) -> str:
        lua = lq.build_set_research(tech_name)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if "RESEARCHING" in result:
            # Verify InGame actually accepted it by comparing tech INDEX.
            # RequestPlayerOperation is fire-and-forget — it can silently no-op
            # while GetResearchingTech() still returns the OLD tech's index (!= -1).
            verify = await self.conn.execute_read(
                f"local me = Game.GetLocalPlayer(); "
                f"local idx = nil; "
                f"for row in GameInfo.Technologies() do "
                f'if row.TechnologyType == "{tech_name}" then idx = row.Index; break end '
                f"end; "
                f"local cur = Players[me]:GetTechs():GetResearchingTech(); "
                f"print(cur == idx and 'MATCH' or 'MISMATCH:'..tostring(cur)..'~='..tostring(idx)); "
                f'print("{lq.SENTINEL}")'
            )
            matched = verify and verify[0] == "MATCH"
            if not matched:
                # InGame silently failed — fall back to GameCore
                gc_lua = lq.build_set_research_gamecore(tech_name)
                gc_lines = await self.conn.execute_read(gc_lua)
                return _action_result(gc_lines)
        return result

    async def set_civic(self, civic_name: str) -> str:
        lua = lq.build_set_civic(civic_name)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if "PROGRESSING" in result:
            # Verify InGame actually accepted it by comparing civic INDEX.
            verify = await self.conn.execute_read(
                f"local me = Game.GetLocalPlayer(); "
                f"local idx = nil; "
                f"for row in GameInfo.Civics() do "
                f'if row.CivicType == "{civic_name}" then idx = row.Index; break end '
                f"end; "
                f"local cur = Players[me]:GetCulture():GetProgressingCivic(); "
                f"print(cur == idx and 'MATCH' or 'MISMATCH:'..tostring(cur)..'~='..tostring(idx)); "
                f'print("{lq.SENTINEL}")'
            )
            matched = verify and verify[0] == "MATCH"
            if not matched:
                # InGame silently failed — fall back to GameCore
                lua_gc = lq.build_set_civic_gamecore(civic_name)
                gc_lines = await self.conn.execute_read(lua_gc)
                return _action_result(gc_lines)
        return result

    # ------------------------------------------------------------------
    # Diplomacy methods
    # ------------------------------------------------------------------

    async def get_diplomacy_sessions(self) -> list[lq.DiplomacySession]:
        lua = lq.build_diplomacy_session_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_diplomacy_sessions(lines)

    async def diplomacy_respond(self, other_player_id: int, response: str) -> str:
        # Capture dialogue text BEFORE response to detect goodbye phase
        pre_sessions = await self.get_diplomacy_sessions()
        pre_text = ""
        for s in pre_sessions:
            if s.other_player_id == other_player_id:
                pre_text = s.dialogue_text
                break

        # Phase 1: Send AddResponse only (no CloseSession — engine handles lifecycle)
        lua = lq.build_diplomacy_respond(other_player_id, response.upper())
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)

        # EXIT and error paths return immediately
        if "SESSION_CLOSED" in result or result.startswith("Error"):
            return result

        # Phase 2: Give engine ~9 frames (0.3s at 30fps) to process the
        # response and transition/close the session, then check state in
        # a separate TCP round-trip (same-frame checks see stale state).
        await asyncio.sleep(0.3)
        check_lines = await self.conn.execute_write(
            lq.build_check_diplomacy_session_state(other_player_id)
        )
        if not any("SESSION_OPEN" in l for l in check_lines):
            return f"OK:RESPONDED|{response.upper()}|SESSION_CLOSED"

        # Phase 3: Session still open — check if dialogue text changed.
        # If unchanged, we're in the goodbye phase. Auto-close.
        post_sessions = await self.get_diplomacy_sessions()
        post_text = ""
        for s in post_sessions:
            if s.other_player_id == other_player_id:
                post_text = s.dialogue_text
                break

        if not post_sessions:
            # Session disappeared between checks (race condition)
            return f"OK:RESPONDED|{response.upper()}|SESSION_CLOSED"

        if post_text == pre_text:
            # Dialogue unchanged → goodbye phase. Force close.
            log.info(
                "Goodbye phase detected (text unchanged) for player %d — auto-closing",
                other_player_id,
            )
            close_lua = lq.build_diplomacy_respond(other_player_id, "EXIT")
            await self.conn.execute_write(close_lua)
            return f"OK:RESPONDED|{response.upper()}|SESSION_CLOSED (auto-closed goodbye phase)"

        # Include the new dialogue text so the agent can see what the leader said
        post_reason = ""
        for s in post_sessions:
            if s.other_player_id == other_player_id:
                post_reason = s.reason_text
                break
        dialogue_note = f'\nLeader says: "{post_text}"'
        if post_reason:
            dialogue_note += f'\nReason/agenda: "{post_reason}"'
        return f"OK:RESPONDED|{response.upper()}|SESSION_CONTINUES{dialogue_note}"

    async def send_diplomatic_action(self, other_player_id: int, action: str) -> str:
        if action.upper() == "OPEN_BORDERS":
            # Session-based OPEN_BORDERS causes AI turn hang.
            # Route through the trade deal API instead (mutual open borders).
            return await self.propose_trade(
                other_player_id,
                offer_items=[{"type": "AGREEMENT", "subtype": "OPEN_BORDERS"}],
                request_items=[{"type": "AGREEMENT", "subtype": "OPEN_BORDERS"}],
            )
        is_war = action.upper().endswith("_WAR") and action.upper().startswith(
            "DECLARE_"
        )
        lua = lq.build_send_diplo_action(other_player_id, action.upper())
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)

        if is_war and not result.startswith("ERR:"):
            # War session left open for ~8s so the leader animation plays.
            # Background task will close session + dismiss DiplomacyActionView.
            asyncio.create_task(self._cleanup_war_diplomacy(other_player_id))

        return result

    async def _cleanup_war_diplomacy(self, other_player_id: int) -> None:
        """Background: dismiss war declaration diplomacy view after animation.

        Two-phase cleanup (must be separate Lua calls — the engine fires
        OnDiplomacySessionClosed asynchronously so the view needs a frame
        to transition from CONVERSATION_MODE to OVERVIEW_MODE):
        1. CloseSession — view transitions to OVERVIEW_MODE
        2. NaturalWonderPopup trick — forces Close() from OVERVIEW_MODE
        """
        await asyncio.sleep(8)
        try:
            # Phase 1: close session → view goes to OVERVIEW_MODE
            lua1 = lq.build_war_close_session(other_player_id)
            await self.conn.execute_write(lua1)

            # Let engine process OnDiplomacySessionClosed
            await asyncio.sleep(1)

            # Phase 2: force-dismiss the OVERVIEW_MODE view
            lua2 = lq.build_war_dismiss_view()
            await self.conn.execute_write(lua2)
        except Exception as e:
            log.warning("War diplomacy cleanup failed: %s", e)

    # ------------------------------------------------------------------
    # Trade deal methods (InGame context)
    # ------------------------------------------------------------------

    async def get_deal_options(self, other_player_id: int) -> lq.DealOptions:
        lua = lq.build_deal_options_query(other_player_id)
        lines = await self.conn.execute_write(lua)
        return lq.parse_deal_options_response(lines)

    async def get_pending_deals(self) -> list[lq.PendingDeal]:
        lua = lq.build_pending_deals_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_pending_deals_response(lines)

    async def respond_to_deal(self, other_player_id: int, accept: bool) -> str:
        lua = lq.build_respond_to_deal(other_player_id, accept)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def propose_trade(
        self,
        other_player_id: int,
        offer_items: list[dict],
        request_items: list[dict],
    ) -> str:
        lua = lq.build_propose_trade(other_player_id, offer_items, request_items)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        # Dismiss diplomacy UI left open by the trade session.
        # After CloseSession, the game transitions DiplomacyActionView to
        # OVERVIEW_MODE (intel screen). Need a brief delay for the C++ UI
        # state machine to settle, then dismiss it in a separate call.
        await asyncio.sleep(0.3)
        try:
            await self.conn.execute_write(
                'pcall(function() ContextPtr:LookUpControl("/InGame/DiplomacyActionView"):SetHide(true) end) '
                "pcall(function() Events.HideLeaderScreen() end) "
                "LuaEvents.DiplomacyActionView_ShowIngameUI() "
                f'print("{lq.SENTINEL}")'
            )
        except Exception:
            pass
        return result

    async def test_trade(
        self,
        other_player_id: int,
        offer_items: list[dict],
        request_items: list[dict],
    ) -> str:
        lua = lq.build_test_trade(other_player_id, offer_items, request_items)
        lines = await self.conn.execute_write(lua)
        result = lq.parse_test_trade_response(lines)
        return narrate_test_trade(result)

    async def propose_peace(self, other_player_id: int) -> str:
        lua = lq.build_propose_peace(other_player_id)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if result.startswith("Error"):
            return result
        # War state is async — verify with a second round-trip
        verify_lines = await self.conn.execute_write(
            lq.build_check_war_state(other_player_id)
        )
        at_peace = any("AT_PEACE" in l for l in verify_lines)
        name = result.split("|", 1)[1] if "|" in result else f"player {other_player_id}"
        if at_peace:
            return f"ACCEPTED|Peace established with {name}"
        else:
            return f"REJECTED|{name} rejected your peace offer"

    async def form_alliance(self, other_player_id: int, alliance_type: str) -> str:
        lua = lq.build_form_alliance(other_player_id, alliance_type.upper())
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Policy methods (InGame context)
    # ------------------------------------------------------------------

    async def get_policies(self) -> lq.GovernmentStatus:
        lua = lq.build_policies_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_policies_response(lines)

    async def set_policies(self, assignments: dict[int, str]) -> str:
        lua = lq.build_set_policies(assignments)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if not result.startswith("Error"):
            # Post-verify: RequestPolicyChanges can silently no-op (e.g. during era transitions)
            status = await self.get_policies()
            slot_map = {s.slot_index: s.current_policy for s in status.slots}
            mismatches = []
            for idx, pol in assignments.items():
                expected = None if pol.upper() == "NONE" else pol
                actual = slot_map.get(idx)
                if actual != expected:
                    wanted = "EMPTY" if expected is None else pol
                    got = actual or "EMPTY"
                    mismatches.append(f"slot {idx} (wanted {wanted}, got {got})")
            if mismatches:
                result += (
                    f"\nWARN:SILENT_FAILURE — engine rejected: {', '.join(mismatches)}. "
                    "Try a different policy or retry next turn."
                )
        return result

    # ------------------------------------------------------------------
    # Governor methods (InGame context)
    # ------------------------------------------------------------------

    async def get_governors(self) -> lq.GovernorStatus:
        lua = lq.build_governors_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_governors_response(lines)

    async def appoint_governor(self, governor_type: str) -> str:
        lua = lq.build_appoint_governor(governor_type)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def assign_governor(self, governor_type: str, city_id: int) -> str:
        lua = lq.build_assign_governor(governor_type, city_id)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def promote_governor(self, governor_type: str, promotion_type: str) -> str:
        lua = lq.build_promote_governor(governor_type, promotion_type)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if "PROMOTED" in result:
            # Verify promotion actually applied (RequestPlayerOperation is async)
            verify = await self.conn.execute_write(
                f"local me = Game.GetLocalPlayer(); "
                f"local pGovs = Players[me]:GetGovernors(); "
                f'local gov = GameInfo.Governors["{governor_type}"]; '
                f'local promo = GameInfo.GovernorPromotions["{promotion_type}"]; '
                f"if gov and promo then "
                f"  local g = pGovs:GetGovernor(gov.Hash); "
                f"  if g and g:HasPromotion(promo.Index) then "
                f'    print("VERIFIED") '
                f"  else "
                f'    print("ERR:PROMOTION_FAILED|{promotion_type} was not applied") '
                f"  end "
                f"else "
                f'  print("ERR:LOOKUP_FAILED") '
                f"end; "
                f'print("{lq.SENTINEL}")'
            )
            if any("ERR:" in l for l in verify):
                return _action_result(verify)
        return result

    # ------------------------------------------------------------------
    # Promotion methods
    # ------------------------------------------------------------------

    async def get_unit_promotions(self, unit_id: int) -> lq.UnitPromotionStatus:
        unit_index = unit_id % 65536
        lua = lq.build_unit_promotions_query(unit_index)
        lines = await self.conn.execute_read(lua)
        return lq.parse_unit_promotions_response(lines)

    async def promote_unit(self, unit_id: int, promotion_type: str) -> str:
        unit_index = unit_id % 65536
        lua = lq.build_promote_unit(unit_index, promotion_type)
        lines = await self.conn.execute_read(lua)  # GameCore context
        result = _action_result(lines)
        # GameCore SetPromotion doesn't clear the InGame NEEDS_PROMOTION
        # notification, which blocks end_turn until dismissed.
        # Use XP-threshold formula (matching end_turn handler) to decide
        # whether any unit still genuinely needs a promotion before dismissing.
        if not result.startswith("Error"):
            try:
                await self.conn.execute_write(
                    f"local me = Game.GetLocalPlayer(); "
                    f"local anyNeed = false; "
                    f"for i, u in Players[me]:GetUnits():Members() do "
                    f"  if u:GetX() ~= -9999 then "
                    f"    local ok, exp = pcall(function() return u:GetExperience() end); "
                    f"    if ok and exp then "
                    f"      local ui = GameInfo.Units[u:GetType()]; "
                    f'      local promClass = ui and ui.PromotionClass or ""; '
                    f'      if promClass ~= "" then '
                    f"        local promoCount = 0; "
                    f"        for p in GameInfo.UnitPromotions() do "
                    f"          if p.PromotionClass == promClass and exp:HasPromotion(p.Index) then "
                    f"            promoCount = promoCount + 1 "
                    f"          end "
                    f"        end; "
                    f"        local t1 = exp:GetExperienceForNextLevel(); "
                    f"        local xp = exp:GetExperiencePoints(); "
                    f"        local needed = t1 * (promoCount + 1) * (promoCount + 2) / 2; "
                    f"        if xp >= needed then "
                    f"          anyNeed = true "
                    f"        else "
                    f"          local stored = 0; "
                    f"          pcall(function() stored = exp:GetStoredPromotions() end); "
                    f"          if stored > 0 then "
                    f"            pcall(function() exp:ChangeStoredPromotions(-stored) end) "
                    f"          end "
                    f"        end "
                    f"      end "
                    f"    end "
                    f"  end "
                    f"  if anyNeed then break end "
                    f"end; "
                    f"if not anyNeed then "
                    f"  local list = NotificationManager.GetList(me); "
                    f"  if list then "
                    f"    for _, nid in ipairs(list) do "
                    f"      local e = NotificationManager.Find(me, nid); "
                    f"      if e and not e:IsDismissed() then "
                    f"        local bt = e:GetEndTurnBlocking(); "
                    f"        if bt and bt == EndTurnBlockingTypes.ENDTURN_BLOCKING_UNIT_PROMOTION then "
                    f"          pcall(function() NotificationManager.SendActivated(me, nid) end); "
                    f"          pcall(function() NotificationManager.Dismiss(me, nid) end) "
                    f"        else "
                    f"          local tn = ''; "
                    f"          pcall(function() tn = e:GetTypeName() end); "
                    f"          if tn == 'NOTIFICATION_UNIT_PROMOTION_AVAILABLE' then "
                    f"            pcall(function() NotificationManager.Dismiss(me, nid) end) "
                    f"          end "
                    f"        end "
                    f"      end "
                    f"    end "
                    f"  end "
                    f"end; "
                    f'print("OK"); print("{lq.SENTINEL}")'
                )
            except Exception:
                pass  # non-fatal — end_turn blocker handler will catch it
        return result

    # ------------------------------------------------------------------
    # City-state / Envoy methods (InGame context)
    # ------------------------------------------------------------------

    async def get_city_states(self) -> lq.EnvoyStatus:
        lua = lq.build_city_states_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_city_states_response(lines)

    async def send_envoy(self, city_state_player_id: int) -> str:
        lua = lq.build_send_envoy(city_state_player_id)
        lines = await self.conn.execute_write(lua)
        result = _action_result(lines)
        if result.startswith("OK:ENVOY_SENT"):
            # Verify token actually decremented (async race condition workaround)
            await asyncio.sleep(0.1)
            try:
                verify_lines = await self.conn.execute_write(
                    f"local me = Game.GetLocalPlayer(); "
                    f"print(Players[me]:GetInfluence():GetTokensToGive()); "
                    f'print("{lq.SENTINEL}")'
                )
                if verify_lines and verify_lines[0].strip().lstrip("-").isdigit():
                    actual = int(verify_lines[0].strip())
                    result += f" (verified remaining: {actual})"
            except Exception:
                log.debug("Envoy verification failed", exc_info=True)
        return result

    # ------------------------------------------------------------------
    # Pantheon methods (InGame context)
    # ------------------------------------------------------------------

    async def get_pantheon_status(self) -> lq.PantheonStatus:
        lua = lq.build_pantheon_status_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_pantheon_status_response(lines)

    async def choose_pantheon(self, belief_type: str) -> str:
        lua = lq.build_choose_pantheon(belief_type)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Religion founding methods (InGame context)
    # ------------------------------------------------------------------

    async def get_religion_founding_status(self) -> lq.ReligionFoundingStatus:
        lua = lq.build_religion_beliefs_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_religion_beliefs_response(lines)

    async def found_religion(
        self, religion_type: str, follower_belief: str, founder_belief: str
    ) -> str:
        lua = lq.build_found_religion(religion_type, follower_belief, founder_belief)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Unit upgrade methods (InGame context)
    # ------------------------------------------------------------------

    async def check_unit_upgrade(self, unit_id: int) -> str:
        unit_index = unit_id % 65536
        lua = lq.build_unit_upgrade_query(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def upgrade_unit(self, unit_id: int) -> str:
        unit_index = unit_id % 65536
        lua = lq.build_upgrade_unit(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Dedications / Commemorations
    # ------------------------------------------------------------------

    async def get_dedications(self) -> lq.DedicationStatus:
        lua = lq.build_dedications_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_dedications_response(lines)

    async def choose_dedication(self, dedication_index: int) -> str:
        lua = lq.build_choose_dedication(dedication_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # District / wonder advisor (with per-turn budget)
    # ------------------------------------------------------------------

    # Pathological loops (Gemini Pro's 1,567 calls in one turn) motivate a
    # per-turn budget. Opus averages 2-4 advisor calls/turn so 20 leaves
    # 5x headroom for legitimate exploration.
    ADVISOR_BUDGET_SOFT = 10
    ADVISOR_BUDGET_HARD = 20

    def _record_advisor_call(self) -> int:
        """Count one advisor query and return the running per-turn total.

        Counting is separate from gating because the implicit advisor call
        behind ``set_city_production`` is exempt from the budget but still
        belongs in the count (see ``_advisor_budget_check``).
        """
        self._advisor_calls_this_turn += 1
        return self._advisor_calls_this_turn

    def _advisor_budget_check(self) -> tuple[str | None, str | None]:
        """Check advisor budget. Returns (hard_error, soft_warning).

        - hard_error: short-circuit string if budget exceeded (caller returns it)
        - soft_warning: string to prepend to the result, or None

        Only the explicit advisor tools gate on this. The implicit call that
        ``set_city_production`` makes when it is given no coordinates counts
        but never gates: the hard cap short-circuits its caller, so charging
        the implicit path would fail a *production* call because of a rate
        limit on a *different* tool it used internally. The cap exists to stop
        advisor-spam-as-search, and the implicit path is by construction one
        advisor call per placement — the behaviour the cap was trying to force.
        """
        # Increment unconditionally — the hard-cap path stays sticky until
        # the end-of-turn reset, and reporting the true call count is more
        # honest for logs and telemetry.
        n = self._record_advisor_call()
        # State the limit and the count. No instruction about how to play:
        # these are Lua-cost guards, not advice, and set_city_production now
        # calls an advisor for the agent anyway.
        if n > self.ADVISOR_BUDGET_HARD:
            return (
                f"ERR:ADVISOR_BUDGET_EXCEEDED|{n} advisor calls this turn, "
                f"limit {self.ADVISOR_BUDGET_HARD}. Resets next turn. "
                f"set_city_production picks a tile without an advisor call "
                f"when you omit target_x/target_y.",
                None,
            )
        if n >= self.ADVISOR_BUDGET_SOFT:
            return (
                None,
                f"ADVISOR BUDGET: {n}/{self.ADVISOR_BUDGET_HARD} advisor "
                f"calls this turn. Resets next turn.",
            )
        return None, None

    async def get_district_advisor(
        self, city_id: int, district_type: str, enforce_budget: bool = True
    ) -> list[lq.DistrictPlacement] | str:
        """Returns placements list, or an error string if placement is impossible.

        ``enforce_budget=False`` counts the call but never gates on it — used
        by the implicit placement path in ``set_city_production``.
        """
        soft_warn = None
        if enforce_budget:
            hard_err, soft_warn = self._advisor_budget_check()
            if hard_err:
                return hard_err
        else:
            self._record_advisor_call()
        lua = lq.build_district_advisor_query(city_id, district_type)
        lines = await self.conn.execute_write(lua)
        # Check for error bail lines (parser only looks for DPLOT| and silently
        # discards errors, losing the actual reason for failure)
        for line in lines:
            if line.startswith("ERR:"):
                return line  # propagate the specific error to the agent
        # Warning only attaches to the success path — error-string returns
        # bypass the server wrapper's narration branch and would otherwise
        # leave a stale warning for the next advisor call.
        self._advisor_budget_warning = soft_warn
        return lq.parse_district_advisor_response(lines)

    async def get_wonder_advisor(
        self, city_id: int, wonder_name: str, enforce_budget: bool = True
    ) -> list[lq.WonderPlacement] | str:
        """Returns placements list, or an error string if budget exceeded.

        ``enforce_budget=False`` counts the call but never gates on it — used
        by the implicit placement path in ``set_city_production``.
        """
        soft_warn = None
        if enforce_budget:
            hard_err, soft_warn = self._advisor_budget_check()
            if hard_err:
                return hard_err
        else:
            self._record_advisor_call()
        lua = lq.build_wonder_advisor_query(city_id, wonder_name)
        lines = await self.conn.execute_write(lua)
        # Warning only attaches to the success path (same reason as above)
        self._advisor_budget_warning = soft_warn
        return lq.parse_wonder_advisor_response(lines)

    # ------------------------------------------------------------------
    # Tile purchase methods (InGame context)
    # ------------------------------------------------------------------

    async def get_purchasable_tiles(self, city_id: int) -> list[lq.PurchasableTile]:
        lua = lq.build_purchasable_tiles_query(city_id)
        lines = await self.conn.execute_write(lua)
        return lq.parse_purchasable_tiles_response(lines)

    async def purchase_tile(self, city_id: int, x: int, y: int) -> str:
        lua = lq.build_purchase_tile(city_id, x, y)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Government change (InGame context)
    # ------------------------------------------------------------------

    async def change_government(self, government_type: str) -> str:
        lua = lq.build_change_government(government_type)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Great People (InGame context)
    # ------------------------------------------------------------------

    async def get_great_people(self) -> list[lq.GreatPersonInfo]:
        lua = lq.build_great_people_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_great_people_response(lines)

    async def get_gp_advisor(
        self, unit_index: int
    ) -> lq.GPAdvisorResult | str | None:
        """Placements, or the game's own reason for refusing, or None.

        The query bails with a specific reason — the unit is not a Great
        Person, its class is unknown — and the parser has no field for one, so
        it returned None and the caller replaced every reason with the same
        guess. That hid a real defect for as long as the tool existed: the
        class lookup read a column that does not exist, so every Great Person
        was reported as "not a Great Person unit".
        """
        lua = lq.build_gp_advisor_query(unit_index)
        lines = await self.conn.execute_write(lua)
        for line in lines:
            if line.startswith("ERR:"):
                return f"Error: {line[4:]}"
        return lq.parse_gp_advisor_response(lines)

    async def recruit_great_person(self, individual_id: int) -> str:
        before = await self._unit_roster()
        lua = lq.build_recruit_great_person(individual_id)
        lines = await self.conn.execute_write(lua)
        result = lines[0] if lines else "No response"
        return result + await self._name_spawned_unit(before, result)

    async def patronize_great_person(
        self, individual_id: int, yield_type: str = "YIELD_GOLD"
    ) -> str:
        before = await self._unit_roster()
        lua = lq.build_patronize_great_person(individual_id, yield_type)
        lines = await self.conn.execute_write(lua)
        result = lines[0] if lines else "No response"
        return result + await self._name_spawned_unit(before, result)

    async def _unit_roster(self) -> dict[int, str]:
        """The local player's unit ids and types, or {} if the read fails."""
        try:
            lines = await self.conn.execute_read(lq.build_unit_roster_query())
            return lq.parse_unit_roster(lines)
        except Exception:
            log.debug("Unit roster read failed", exc_info=True)
            return {}

    async def _name_spawned_unit(self, before: dict[int, str], result: str) -> str:
        """Report the unit id a successful recruit or patronize created.

        A Great Person spawns in the capital, so the agent otherwise has to
        rescan get_units and work out which unit is new. The operation is
        fire-and-forget, so the unit can lag the response by a moment; hence
        the retries. Reports nothing unless exactly one unit appeared, because
        anything else means this cannot say which one it was.
        """
        if not result.startswith("OK:"):
            return ""

        for attempt in range(3):
            if attempt:
                await asyncio.sleep(0.3)
            after = await self._unit_roster()
            spawned = set(after) - set(before)
            if len(spawned) == 1:
                unit_id = spawned.pop()
                return f"|unit_id:{unit_id}|{after[unit_id]}"
        return ""

    async def get_religion_status(self) -> lq.ReligionStatus:
        lines = await self.conn.execute_write(lq.build_religion_status_query())
        return lq.parse_religion_status_response(lines)

    async def reject_great_person(self, individual_id: int) -> str:
        lua = lq.build_reject_great_person(individual_id)
        lines = await self.conn.execute_write(lua)
        return lines[0] if lines else "No response"

    # ------------------------------------------------------------------
    # Trade route methods (InGame context)
    # ------------------------------------------------------------------

    async def get_trade_routes(self) -> lq.TradeRouteStatus:
        lua = lq.build_trade_routes_query()
        lines = await self.conn.execute_write(
            lua
        )  # InGame context (GetOutgoingRoutes is InGame-only)
        return lq.parse_trade_routes_response(lines)

    async def get_trade_destinations(
        self, unit_index: int
    ) -> list[lq.TradeDestination]:
        lua = lq.build_trade_destinations_query(unit_index)
        lines = await self.conn.execute_write(lua)
        return lq.parse_trade_destinations_response(lines)

    async def make_trade_route(
        self, unit_index: int, target_x: int, target_y: int
    ) -> str:
        lua = lq.build_make_trade_route(unit_index, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Great Person activation (InGame context)
    # ------------------------------------------------------------------

    async def activate_great_person(self, unit_index: int) -> str:
        lua = lq.build_activate_great_person(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def spread_religion(self, unit_index: int) -> str:
        lua = lq.build_spread_religion(unit_index)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Trader teleport (InGame context)
    # ------------------------------------------------------------------

    async def teleport_to_city(
        self, unit_index: int, target_x: int, target_y: int
    ) -> str:
        lua = lq.build_teleport_to_city(unit_index, target_x, target_y)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # World Congress (InGame context)
    # ------------------------------------------------------------------

    async def get_world_congress(self) -> lq.WorldCongressStatus:
        lua = lq.build_world_congress_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_world_congress_response(lines)

    async def vote_world_congress(
        self, resolution_hash: int, option: int, target_index: int, num_votes: int
    ) -> str:
        lua = lq.build_congress_vote(resolution_hash, option, target_index, num_votes)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def submit_congress(self) -> str:
        lua = lq.build_congress_submit()
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    async def queue_wc_votes(self, votes: list[dict]) -> str:
        """Store agent voting preferences and register WC event handler."""
        lua = lq.build_register_wc_voter(votes=votes)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # City yield focus (InGame context)
    # ------------------------------------------------------------------

    async def set_city_focus(self, city_id: int, focus: str) -> str:
        lua = lq.build_set_yield_focus(city_id, focus)
        lines = await self.conn.execute_write(lua)
        return _action_result(lines)

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    async def get_notifications(self) -> list[lq.GameNotification]:
        lua = lq.build_notifications_query()
        lines = await self.conn.execute_write(lua)
        return lq.parse_notifications_response(lines)

    # ------------------------------------------------------------------
    # Snapshot-diff for turn event detection
    # ------------------------------------------------------------------

    async def _take_snapshot(
        self, overview: lq.GameOverview | None = None
    ) -> lq.TurnSnapshot:
        """Capture current game state for diffing."""
        if overview is None:
            ov_lines = await self.conn.execute_write(lq.build_overview_query())
            overview = lq.parse_overview_response(ov_lines)

        unit_lines = await self.conn.execute_read(lq.build_units_query())
        units = lq.parse_units_response(unit_lines)

        city_lines = await self.conn.execute_write(lq.build_cities_query())
        cities, _ = lq.parse_cities_response(city_lines)

        try:
            stk_lines = await self.conn.execute_write(lq.build_stockpile_query())
            stockpiles = lq.parse_stockpile_response(stk_lines)
        except Exception:
            log.debug("Stockpile query failed", exc_info=True)
            stockpiles = []

        return lq.TurnSnapshot(
            turn=overview.turn,
            units={u.unit_id: u for u in units},
            cities={
                c.city_id: lq.CitySnapshot(
                    city_id=c.city_id,
                    name=c.name,
                    population=c.population,
                    currently_building=c.currently_building,
                    food_surplus=c.food_surplus,
                    turns_to_grow=c.turns_to_grow,
                    loyalty=c.loyalty,
                    loyalty_per_turn=c.loyalty_per_turn,
                )
                for c in cities
            },
            current_research=overview.current_research,
            current_civic=overview.current_civic,
            stockpiles=stockpiles,
        )

    @staticmethod
    def _diff_snapshots(
        before: lq.TurnSnapshot, after: lq.TurnSnapshot
    ) -> list[lq.TurnEvent]:
        """Compare two snapshots and generate events."""
        events: list[lq.TurnEvent] = []

        # --- Unit events ---
        for uid, ub in before.units.items():
            if uid not in after.units:
                events.append(
                    lq.TurnEvent(
                        priority=1,
                        category="unit",
                        message=f"Your {ub.name} ({ub.unit_type}) was killed! Last seen at ({ub.x},{ub.y}).",
                    )
                )
            else:
                ua = after.units[uid]
                dmg = ub.health - ua.health
                if dmg > 0:
                    events.append(
                        lq.TurnEvent(
                            priority=2,
                            category="unit",
                            message=f"Your {ua.name} ({ua.unit_type}) took {dmg} damage! HP: {ua.health}/{ua.max_health} at ({ua.x},{ua.y}).",
                        )
                    )
                elif dmg < 0:
                    events.append(
                        lq.TurnEvent(
                            priority=3,
                            category="unit",
                            message=f"Your {ua.name} ({ua.unit_type}) healed {-dmg} HP. HP: {ua.health}/{ua.max_health}.",
                        )
                    )

        for uid, ua in after.units.items():
            if uid not in before.units:
                events.append(
                    lq.TurnEvent(
                        priority=3,
                        category="unit",
                        message=f"New unit: {ua.name} ({ua.unit_type}) at ({ua.x},{ua.y}).",
                    )
                )

        # --- City events ---
        for cid, cb in before.cities.items():
            if cid not in after.cities:
                events.append(
                    lq.TurnEvent(
                        priority=1,
                        category="city",
                        message=f"City {cb.name} was lost!",
                    )
                )
            else:
                ca = after.cities[cid]
                if ca.population > cb.population:
                    events.append(
                        lq.TurnEvent(
                            priority=3,
                            category="city",
                            message=f"{ca.name} grew to population {ca.population}.",
                        )
                    )
                if (
                    cb.currently_building != "NONE"
                    and ca.currently_building != cb.currently_building
                ):
                    now = ca.currently_building
                    if now in ("NONE", "nothing"):
                        now = "nothing"
                    elif now == "CORRUPTED_QUEUE":
                        now = "nothing (queue invalidated — set new production)"
                    events.append(
                        lq.TurnEvent(
                            priority=2,
                            category="city",
                            message=f"{ca.name} finished building {cb.currently_building}. Now: {now}.",
                        )
                    )

        for cid, ca in after.cities.items():
            if cid not in before.cities:
                events.append(
                    lq.TurnEvent(
                        priority=2,
                        category="city",
                        message=f"New city founded: {ca.name}!",
                    )
                )

        # --- Research/civic events ---
        if (
            before.current_research != "None"
            and after.current_research != before.current_research
        ):
            events.append(
                lq.TurnEvent(
                    priority=2,
                    category="research",
                    message=f"Research complete: {before.current_research}! Now: {after.current_research}.",
                )
            )

        if (
            before.current_civic != "None"
            and after.current_civic != before.current_civic
        ):
            events.append(
                lq.TurnEvent(
                    priority=2,
                    category="civic",
                    message=f"Civic complete: {before.current_civic}! Now: {after.current_civic}.",
                )
            )

        # --- Stockpile events ---
        before_stk = {s.name: s for s in before.stockpiles}
        after_stk = {s.name: s for s in after.stockpiles}
        for name, sa in after_stk.items():
            sb = before_stk.get(name)
            if sb and sb.amount > 0 and sa.amount == 0:
                net = sa.per_turn - sa.demand + sa.imported
                events.append(
                    lq.TurnEvent(
                        priority=2,
                        category="resources",
                        message=f"DEPLETED: {name} stockpile hit 0 ({net:+d}/t) — units requiring {name} may be disbanded.",
                    )
                )

        events.sort(key=lambda e: e.priority)
        return events

    @staticmethod
    def _build_turn_report(
        turn_before: int,
        turn_after: int,
        events: list[lq.TurnEvent],
        notifications: list[lq.GameNotification],
        stockpiles: list[lq.ResourceStockpile] | None = None,
        score: int | None = None,
    ) -> str:
        """Format turn events and notifications into a scannable report."""
        header = f"Turn {turn_before} -> {turn_after}"
        if score is not None:
            header += f" | Score: {score}"
        lines = [header]

        if stockpiles:
            visible = [
                s for s in stockpiles if s.amount > 0 or s.per_turn > 0 or s.demand > 0
            ]
            if visible:
                parts = []
                for s in visible:
                    net = s.per_turn - s.demand + s.imported
                    parts.append(f"{s.name} {s.amount}/{s.cap} ({net:+d}/t)")
                lines.append(f"Resources: {', '.join(parts)}")

        if events:
            lines.append("")
            lines.append("== Events ==")
            icons = {1: "!!!", 2: ">>", 3: "--"}
            for e in events:
                icon = icons.get(e.priority, "--")
                lines.append(f"  {icon} {e.message}")

        # Use the enriched is_action_required field from the parser
        action_required = [n for n in notifications if n.is_action_required]
        # Only show informational notifications from the last 2 turns — older ones
        # are stale (e.g. "Wonder Completed" from 3 turns ago) and clutter the report.
        recent_cutoff = (turn_after or 0) - 2
        info_notifs = [
            n
            for n in notifications
            if not n.is_action_required and n.turn >= recent_cutoff
        ]

        if action_required:
            lines.append("")
            lines.append("== Action Required ==")
            for n in action_required:
                hint = f"  -> Use: {n.resolution_hint}" if n.resolution_hint else ""
                lines.append(f"  * {n.message}{hint}")

        if info_notifs:
            lines.append("")
            lines.append("== Notifications ==")
            for n in info_notifs:
                lines.append(f"  - {n.message}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Turn management
    # ------------------------------------------------------------------

    async def end_turn(self) -> str:
        """End the turn with snapshot-diff event detection."""
        from civ_mcp.end_turn import execute_end_turn

        return await execute_end_turn(self)

    async def dismiss_popup(self) -> str:
        """Dismiss any blocking popup or UI overlay."""
        from civ_mcp.game_lifecycle import dismiss_popup

        return await dismiss_popup(self.conn)

    async def ensure_no_blocking_popup(self) -> None:
        """Clear a popup that would otherwise swallow the next command.

        A popup blocks the InGame context, so a command issued underneath one
        is accepted and then silently discarded. Every mutating tool runs this
        first, which is why no tool asks the agent to dismiss popups itself.

        Two round trips at worst. The poll is one, and reports CLEAR, POPUP, or
        CRITICAL; the shallow dismissal is the second, and only on POPUP.
        CRITICAL means a diplomacy screen is open: the agent may be part-way
        through answering it, and force-closing the session leaves the AI
        diplomacy subsystem inconsistent. That is the guard PopupWatcher
        applies, inherited here.

        Shallow (`deep=False`) on purpose: the deep phases probe up to 150 Lua
        states one at a time, which is minutes of round trips across a turn of
        unit orders, to reach popups that do not swallow commands anyway.
        PopupWatcher clears those on its own timer.

        Never raises. A failed poll must not turn into a failed tool call.
        """
        from civ_mcp.game_lifecycle import dismiss_popup
        from civ_mcp.spectator import _POPUP_POLL_LUA

        try:
            lines = await self.conn.execute_write(_POPUP_POLL_LUA, timeout=2.0)
        except Exception:
            return

        status = "CLEAR"
        for line in lines:
            stripped = line.strip()
            if stripped in ("POPUP", "CRITICAL", "CLEAR"):
                status = stripped
                break

        if status != "POPUP":
            return

        try:
            await dismiss_popup(self.conn, deep=False)
        except Exception:
            log.debug("Pre-command popup dismissal failed", exc_info=True)

    async def list_saves(self) -> str:
        """List available save files."""
        from civ_mcp.game_lifecycle import list_saves

        return await list_saves(self.conn)

    async def load_game_save(self, save_name: str) -> str:
        """Load a save file by name (no get_saves prerequisite)."""
        from civ_mcp.game_lifecycle import load_game_save

        result = await load_game_save(self.conn, save_name)
        if not result.startswith(("Error", "ERR", "FAILED")):
            self._record_save_load(save_name)
        return result

    def _record_save_load(self, save_name: str) -> None:
        """Record a successful save load for scumming detection."""
        import time

        # An older save has revealed fewer tiles than the set we carry, so
        # re-seed on the next move rather than under-reporting discoveries.
        self._revealed = None
        ts = time.time()
        turn = self._high_water_turn
        self._save_load_history.append((ts, turn, save_name))
        # Keep bounded
        if len(self._save_load_history) > 50:
            self._save_load_history = self._save_load_history[-50:]

    async def execute_lua(self, code: str, context: str = "gamecore") -> str:
        """Escape hatch: run arbitrary Lua code."""
        from civ_mcp.game_lifecycle import execute_lua

        return await execute_lua(self.conn, code, context)


def _action_result(lines: list[str]) -> str:
    """Parse OK:/ERR: prefixed action responses.

    Scans all lines for the first OK:/ERR: prefix, since LuaEvent
    callbacks (e.g. ShowIngameUI → BulkHide debug prints) can inject
    spurious output before the actual result line.
    """
    if not lines:
        return "Action completed (no response)."
    for line in lines:
        if line.startswith("OK:"):
            return line[3:]
        if line.startswith("ERR:"):
            return f"Error: {line[4:]}"
    # No OK/ERR found — return all lines for debugging
    return "\n".join(lines)


def _is_missing_coords(lines: list[str]) -> bool:
    """True when the game refused an item because it needs a placement tile.

    Emitted by build_produce_item for a wonder given no coordinates. The bail
    happens before RequestOperation, so nothing has been mutated.
    """
    for line in lines:
        if line.startswith("ERR:MISSING_COORDS"):
            return True
    return False


def _format_attack_followup(lines: list[str], attacker_owner: int = 0) -> str:
    """Format the GameCore follow-up read after an attack.

    Filters out units belonging to ``attacker_owner`` so that after a melee
    kill (where the attacker moves onto the target tile) we don't misreport
    our own unit's HP as the defender's.

    Also includes city wall/garrison HP when attacking a walled city.
    """
    parts = []
    for line in lines:
        if line.startswith("UNIT|"):
            fields = line.split("|")
            if len(fields) >= 4:
                # fields: UNIT|TYPE|hp/max|owner:N
                owner_str = fields[3]  # "owner:N"
                try:
                    owner_id = int(owner_str.split(":")[1])
                except (IndexError, ValueError):
                    owner_id = -1
                label = "(yours) " if owner_id == attacker_owner else ""
                parts.append(f"{label}{fields[1]} {fields[2]}")
            elif len(fields) >= 3:
                parts.append(f"{fields[1]} {fields[2]}")
    city_def = _extract_city_defense(lines)
    if city_def:
        wall_hp, wall_max, gar_hp, gar_max = city_def
        if wall_max > 0:
            parts.append(f"Walls {wall_hp}/{wall_max}")
        if gar_max > 0:
            parts.append(f"City garrison {gar_hp}/{gar_max}")
    if not parts:
        return "Target eliminated"
    return ", ".join(parts)


def _extract_pre_hp(result: str) -> int | None:
    """Extract pre-attack enemy HP from attack result line."""
    import re

    # Ranged/city: pre_hp:80/100
    m = re.search(r"pre_hp:(\d+)/", result)
    if m:
        return int(m.group(1))
    # Melee: enemy HP:100 -> 80/100
    m = re.search(r"enemy HP:(\d+) ->", result)
    if m:
        return int(m.group(1))
    return None


def _extract_post_hp(followup_lines: list[str], attacker_owner: int = 0) -> int | None:
    """Extract post-combat *enemy* HP from followup query lines.

    Followup format: UNIT|UNIT_TYPE|hp/max|owner:N
    Skips units belonging to ``attacker_owner`` (after melee kill, attacker
    occupies the target tile and would otherwise be misread as defender).
    Returns HP of first enemy unit found (None if eliminated).
    """
    for line in followup_lines:
        if line.startswith("UNIT|"):
            parts = line.split("|")
            if len(parts) >= 4:
                try:
                    owner_id = int(parts[3].split(":")[1])
                except (IndexError, ValueError):
                    owner_id = -1
                if owner_id == attacker_owner:
                    continue  # our unit, not the target
            if len(parts) >= 3:
                hp_part = parts[2].split("/")[0]
                try:
                    return int(hp_part)
                except ValueError:
                    pass
    return None


def _extract_city_defense(
    followup_lines: list[str],
) -> tuple[int, int, int, int] | None:
    """Extract wall and garrison HP from CITY_DEF followup line.

    Returns ``(wall_hp, wall_max, garrison_hp, garrison_max)`` or *None*
    when the target tile has no city defenses.
    """
    for line in followup_lines:
        if line.startswith("CITY_DEF|"):
            # CITY_DEF|wall:74/100|garrison:197/200
            wall_hp = wall_max = gar_hp = gar_max = 0
            for part in line.split("|")[1:]:
                if part.startswith("wall:"):
                    hp, mx = part[5:].split("/")
                    wall_hp, wall_max = int(hp), int(mx)
                elif part.startswith("garrison:"):
                    hp, mx = part[9:].split("/")
                    gar_hp, gar_max = int(hp), int(mx)
            return (wall_hp, wall_max, gar_hp, gar_max)
    return None
