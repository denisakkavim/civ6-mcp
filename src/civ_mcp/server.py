"""MCP server for Civilization VI — lets LLM agents read game state and play.

Uses FastMCP with the lifespan pattern to maintain a persistent TCP connection
to the running game via FireTuner protocol.
"""

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Generator, Literal, Optional

from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, Field

from civ_mcp import game_launcher
from civ_mcp import narrate as nr
from civ_mcp import recording
from civ_mcp.connection import GameConnection, LuaError
from civ_mcp.game_state import GameState
from civ_mcp.logger import GameLogger
from civ_mcp.spectator import CameraController, PopupWatcher

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    game: GameState
    logger: GameLogger
    camera: CameraController
    popup_watcher: PopupWatcher


async def _auto_boot(conn: GameConnection, save_name: str) -> None:
    """Launch game and load a save before MCP tools become available.

    Called during lifespan when CIV_MCP_SAVE_FILE is set (eval mode).
    Blocks until the game is loaded and ready for play.
    """
    import glob

    from civ_mcp.game_lifecycle import load_game_save

    # 0. Clear stale MCP autosaves. These are the saves that the main
    # menu's "Continue Game" button would load. If a previous run
    # crashed at T197, "Continue Game" resumes T197 instead of loading
    # the scenario save. Clearing them makes "Continue Game" harmless
    # (it would load the scenario save or nothing).
    # This does NOT break --resume-save which loads by name via Lua.
    stale = glob.glob(os.path.join(game_launcher.SINGLE_SAVE_DIR, "0_MCP_*.Civ6Save"))
    if stale:
        for f in stale:
            try:
                os.remove(f)
            except OSError:
                pass
        log.info("Auto-boot: cleared %d stale MCP autosave(s)", len(stale))

    # 1. Launch game (or reuse if already running).
    # The eval runner's ensure_game_ready() typically launches the game
    # before the MCP server starts. _launch_game_sync() detects an
    # already-running game and returns immediately, avoiding a wasteful
    # kill + relaunch cycle through the Aspyr launcher.
    # The step-5 verification below catches wrong-save scenarios as a
    # safety net (the Lua load path fails when mid-session, not from
    # main menu).
    log.info("Auto-boot: launching game...")
    result = await asyncio.to_thread(game_launcher._launch_game_sync)
    log.info("Auto-boot: launch result: %s", result)

    # 2. Connect to FireTuner (retry — game takes time to start)
    for attempt in range(90):
        try:
            await conn.connect()
            log.info("Auto-boot: connected to FireTuner")
            break
        except ConnectionError:
            if attempt % 10 == 0:
                log.info("Auto-boot: waiting for FireTuner... (%ds)", attempt)
            await asyncio.sleep(1)
    else:
        log.error("Auto-boot: could not connect to FireTuner after 90s")
        return

    # 2b. Verify Lua states exist (port can open before game initialises).
    # A hung splash screen ("Loading, Please Wait...") has port open but
    # GameCore never appears. Skip this check — the main menu legitimately
    # has no GameCore on any platform; it only appears after a save is
    # loaded (step 3). The splash hang detection was causing false kills
    # when autosaves were cleaned (game stays at main menu, no GameCore).
    if conn.gamecore_index is None and False:  # disabled — see comment above
        log.warning(
            "Auto-boot: FireTuner connected but GameCore not found "
            "— game may be hung at splash screen"
        )
        for retry in range(30):
            await asyncio.sleep(2)
            try:
                await conn.reconnect()
                if conn.gamecore_index is not None:
                    log.info("Auto-boot: GameCore found after %ds", (retry + 1) * 2)
                    break
            except ConnectionError:
                pass
        else:
            log.error("Auto-boot: GameCore never appeared — killing hung game")
            await asyncio.to_thread(game_launcher._kill_game_sync)
            await asyncio.sleep(5)
            result = await asyncio.to_thread(game_launcher._launch_game_sync)
            log.info("Auto-boot: relaunched after hung splash: %s", result)
            for attempt in range(90):
                try:
                    await conn.connect()
                    if conn.gamecore_index is not None:
                        log.info("Auto-boot: GameCore found on relaunch")
                        break
                except ConnectionError:
                    pass
                await asyncio.sleep(1)
            if conn.gamecore_index is None:
                log.error("Auto-boot: relaunch also failed — giving up")
                return

    # 3. Load save (Lua on Windows/macOS, OCR menu nav on Linux)
    log.info("Auto-boot: loading save '%s'...", save_name)
    result = await load_game_save(conn, save_name)
    log.info("Auto-boot: load result: %s", result)

    # 4. Wait for save to load, click through leader intro, then reconnect.
    # The CONTINUE GAME button on the leader screen has low-contrast
    # teal-on-teal text that OCR often misses — fall back to positional
    # click grid if OCR fails. Verify the click actually worked by
    # checking for Lua states (only available once in-game, not on leader
    # screen).
    log.info("Auto-boot: waiting 15s for save to load...")
    await asyncio.sleep(15)
    clicked = await asyncio.to_thread(
        lambda: game_launcher._click_text("CONTINUE", timeout=105, post_delay=1),
    )
    if clicked:
        log.info("Auto-boot: clicked CONTINUE GAME via OCR")
    else:
        log.warning("Auto-boot: OCR missed CONTINUE — using positional click grid")
        await asyncio.to_thread(game_launcher._click_continue_positional)

    # Verify the click worked — Lua states only appear once past the
    # leader screen into gameplay. Retry positional click if needed.
    await asyncio.sleep(3)
    game_ready = False
    for attempt in range(45):
        try:
            await conn.reconnect()
            if conn.gamecore_index is not None:
                log.info("Auto-boot: game ready (GameCore=%s)", conn.gamecore_index)
                game_ready = True
                break
        except ConnectionError:
            pass
        # Retry positional click every 10s in case the first click missed
        if attempt > 0 and attempt % 10 == 0:
            if not clicked:
                log.info("Auto-boot: retrying positional click (attempt %d)", attempt)
                await asyncio.to_thread(game_launcher._click_continue_positional)
        await asyncio.sleep(1)
    if not game_ready:
        log.warning("Auto-boot: save may not have loaded — GameCore not found")
        return

    # 5. Verify correct save loaded. If the wrong save loaded (e.g.
    # main-menu "Continue Game" loaded a stale autosave instead of the
    # scenario save), reload the correct one via Lua — no OCR needed.
    try:
        verify = await conn.execute_read(
            "local t = Game.GetCurrentGameTurn(); "
            'print("VERIFY|" .. t); '
            'print("---END---")'
        )
        for line in verify:
            if line.startswith("VERIFY|"):
                turn = int(line.split("|")[1])
                if turn > 5:
                    log.error(
                        "Auto-boot: loaded T%d but expected T1 — wrong save! "
                        "Reloading '%s' via Lua",
                        turn,
                        save_name,
                    )
                    # Retry via Lua (Network.LoadGame) — bypasses OCR entirely
                    result = await load_game_save(conn, save_name)
                    log.info("Auto-boot: Lua reload result: %s", result)
                    await asyncio.sleep(15)
                    # Click CONTINUE again for the leader screen
                    await asyncio.to_thread(game_launcher._click_continue_positional)
                    await asyncio.sleep(5)
                    for retry in range(30):
                        try:
                            await conn.reconnect()
                            if conn.gamecore_index is not None:
                                break
                        except ConnectionError:
                            pass
                        await asyncio.sleep(1)
                    # Verify again
                    try:
                        verify2 = await conn.execute_read(
                            "local t = Game.GetCurrentGameTurn(); "
                            'print("VERIFY|" .. t); '
                            'print("---END---")'
                        )
                        for line2 in verify2:
                            if line2.startswith("VERIFY|"):
                                t2 = int(line2.split("|")[1])
                                if t2 > 5:
                                    log.error(
                                        "Auto-boot: Lua reload also loaded T%d "
                                        "— falling back to kill + OCR",
                                        t2,
                                    )
                                    await game_launcher.kill_game()
                                    r = await asyncio.to_thread(
                                        game_launcher._launch_game_sync
                                    )
                                    log.info("Auto-boot: relaunch: %s", r)
                                    r = await asyncio.to_thread(
                                        game_launcher._navigate_to_save_sync,
                                        save_name,
                                        None,
                                    )
                                    log.info("Auto-boot: OCR nav: %s", r)
                                    for a in range(30):
                                        try:
                                            await conn.reconnect()
                                            if conn.gamecore_index is not None:
                                                return
                                        except ConnectionError:
                                            pass
                                        await asyncio.sleep(1)
                                    log.warning("Auto-boot: all fallbacks failed")
                                    return
                                log.info("Auto-boot: Lua reload verified at T%d", t2)
                    except Exception:
                        log.debug("Auto-boot: post-reload verify failed", exc_info=True)
                    return
                log.info("Auto-boot: verified save at T%d", turn)
    except Exception:
        log.debug("Auto-boot: save verification failed", exc_info=True)


# ---------------------------------------------------------------------------
# Testability seam
# ---------------------------------------------------------------------------
# `lifespan` constructs its own dependencies, which puts the whole tool layer
# out of reach of a test unless a real game is listening on a socket. These
# hooks are the only supported way to substitute them. Production never touches
# them; `tests/conftest.py` drives them through `testing_overrides()`.
#
# Background services are opt-out because both poll the connection on a timer.
# Left running against a stubbed connection they interleave unrequested traffic
# with the tool's own, which makes recorded and replayed call sequences
# non-deterministic.

_connection_factory: Callable[[], GameConnection] = GameConnection
_background_services_enabled: bool = True
_log_dir: Path | None = None


@contextmanager
def testing_overrides(
    *,
    connection_factory: Callable[[], GameConnection],
    background_services: bool = False,
    log_dir: Path | None = None,
) -> Generator[None, None, None]:
    """Substitute lifespan dependencies for the duration of the block."""
    global _connection_factory, _background_services_enabled, _log_dir
    previous = (_connection_factory, _background_services_enabled, _log_dir)
    _connection_factory = connection_factory
    _background_services_enabled = background_services
    _log_dir = log_dir
    try:
        yield
    finally:
        _connection_factory, _background_services_enabled, _log_dir = previous


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    conn = _connection_factory()

    logger = GameLogger(log_dir=_log_dir)
    gs = GameState(conn)
    log.info("Tool-call log: %s", logger._path)

    # Cassette recording, off unless CIV_MCP_RECORD names a directory.
    recording.configure_from_env()

    # Auto-boot: launch game + load save when running as eval
    save_file = os.environ.get("CIV_MCP_SAVE_FILE")
    if save_file:
        await _auto_boot(conn, save_file)

    # Spectator-mode background services (camera tracking + popup auto-dismiss)
    camera = CameraController(conn)
    popup_watcher = PopupWatcher(conn)
    if _background_services_enabled:
        camera.start()
        popup_watcher.start()

    try:
        yield AppContext(
            game=gs,
            logger=logger,
            camera=camera,
            popup_watcher=popup_watcher,
        )
    finally:
        await camera.stop()
        await popup_watcher.stop()
        await conn.disconnect()


mcp = FastMCP(
    "Civilization VI",
    instructions=(
        "Read game state and issue commands to a running Civ 6 game. Call "
        "get_game_overview first to orient yourself.\n"
        "Coordinates: a higher y is further south, a lower y is further north.\n"
        "Identifiers: unit ids always come from get_units, city ids always "
        "from get_cities."
    ),
    lifespan=lifespan,
)


def _get_game(ctx: Context) -> GameState:
    return ctx.request_context.lifespan_context.game


def _get_logger(ctx: Context) -> GameLogger:
    return ctx.request_context.lifespan_context.logger


def _get_camera(ctx: Context) -> CameraController:
    return ctx.request_context.lifespan_context.camera


# A fully-qualified game identifier already states its own category:
# DISTRICT_CAMPUS is a district. Asking the agent to say so a second time is a
# parameter it can only get wrong, so the tools infer it from the prefix.
PRODUCIBLE_CATEGORIES = {
    "UNIT_": "UNIT",
    "BUILDING_": "BUILDING",
    "DISTRICT_": "DISTRICT",
    "PROJECT_": "PROJECT",
}

PURCHASABLE_CATEGORIES = {
    "UNIT_": "UNIT",
    "BUILDING_": "BUILDING",
}


def _category_from_prefix(item_type: str, categories: dict[str, str]) -> str | None:
    """The category a game identifier belongs to, or None if the prefix is unknown."""
    for prefix, category in categories.items():
        if item_type.startswith(prefix):
            return category
    return None


def _unknown_prefix_error(item_type: str, categories: dict[str, str]) -> str:
    expected = ", ".join(sorted(categories))
    return (
        f"Error: cannot tell what '{item_type}' is. Expected a fully-qualified "
        f"identifier starting with one of: {expected}"
    )


def _param_summary(params: dict[str, Any]) -> str:
    """Compact one-line summary of tool params for console logging."""
    if not params:
        return ""
    parts = []
    for k, v in params.items():
        s = str(v)
        if len(s) > 40:
            s = s[:37] + "..."
        parts.append(f"{k}={s}")
    return " ".join(parts)


def _result_summary(result: str) -> str:
    """First meaningful line of a result, truncated."""
    line = result.split("\n", 1)[0].strip()
    return line[:120] + "..." if len(line) > 120 else line


async def _logged(
    ctx: Context,
    tool_name: str,
    params: dict[str, Any],
    fn: Callable[[], Awaitable[str]],
    mutating: bool = False,
) -> str:
    """Run a tool function with timing, error handling, and logging.

    `mutating=True` marks a tool that issues game commands. Those are cleared
    of blocking popups first: a popup silently swallows a command in the
    InGame context, so without this the tool reports a success the game never
    performed. Reads go to a different context and are unaffected.
    """
    logger = _get_logger(ctx)
    turn = logger._turn or "?"
    start = time.monotonic()
    # Cassette recording brackets the call so every Lua round trip the tool
    # issues is attributed to it. No-op unless CIV_MCP_RECORD is set.
    recording.begin(tool_name, params)
    try:
        if mutating:
            await _get_game(ctx).ensure_no_blocking_popup()
        result = await fn()
    except (LuaError, ValueError) as e:
        result = f"Error: {e}"
        ms = int((time.monotonic() - start) * 1000)
        log.info(
            "[T%s] %s(%s) ERR %dms: %s",
            turn,
            tool_name,
            _param_summary(params),
            ms,
            _result_summary(result),
        )
        await logger.log_error(tool_name, result)
        recording.finish(result)
        return result
    except ConnectionError as e:
        result = str(e)
        ms = int((time.monotonic() - start) * 1000)
        log.info(
            "[T%s] %s(%s) ERR %dms: %s",
            turn,
            tool_name,
            _param_summary(params),
            ms,
            _result_summary(result),
        )
        await logger.log_error(tool_name, result)

        # Connection-loss recovery: after consecutive failures,
        # the game has likely crashed. Auto-restart from autosave.
        _logged._conn_errors = getattr(_logged, "_conn_errors", 0) + 1
        if _logged._conn_errors >= 5:
            log.error(
                "CONNECTION RECOVERY: %d consecutive connection failures "
                "— restarting the game",
                _logged._conn_errors,
            )
            _logged._conn_errors = 0
            try:
                from civ_mcp.autosave import get_autosave_for_turn, get_latest_autosave

                turn_num = logger._turn
                save = (
                    get_autosave_for_turn(int(turn_num))
                    if turn_num
                    else get_latest_autosave()
                )
                restart_result = await game_launcher.restart_and_load(save)
                log.info("CONNECTION RECOVERY: %s", restart_result)
                gs = _get_game(ctx)
                for rc_attempt in range(30):
                    try:
                        await gs.conn.reconnect()
                        if gs.conn.gamecore_index is not None:
                            log.info("CONNECTION RECOVERY: reconnected")
                            break
                    except ConnectionError:
                        pass
                    await asyncio.sleep(1)
            except Exception:
                log.error("CONNECTION RECOVERY: restart failed", exc_info=True)

        recording.finish(result)
        return result
    # Success — reset connection error counter
    _logged._conn_errors = 0
    ms = int((time.monotonic() - start) * 1000)
    log.info(
        "[T%s] %s(%s) OK %dms: %s",
        turn,
        tool_name,
        _param_summary(params),
        ms,
        _result_summary(result),
    )
    await logger.log_tool_call(tool_name, params, result, ms)
    recording.finish(result)
    return result


# ---------------------------------------------------------------------------
# Query tools (read-only)
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_game_overview(ctx: Context) -> str:
    """Get a high-level summary of the current game state.

    Returns turn number, civilization, yields (gold/science/culture/faith),
    current research and civic, and counts of cities and units.
    Call this first to orient yourself.
    """
    gs = _get_game(ctx)

    async def _run():
        ov = await gs.get_game_overview()
        logger = _get_logger(ctx)
        logger.set_turn(ov.turn)
        try:
            civ, seed = await gs.get_game_identity()
            logger.bind_game(civ, seed)
        except Exception:
            pass
        text = nr.narrate_overview(ov)
        # Check for game-over state
        gameover = await gs.check_game_over()
        if gameover is not None:
            vtype = (
                gameover.victory_type.replace("VICTORY_", "").replace("_", " ").title()
            )
            if gameover.is_defeat:
                text += (
                    f"\n\n*** GAME OVER — DEFEAT ***\n"
                    f"{gameover.winner_leader} of {gameover.winner_name} won a {vtype} victory.\n"
                    f"No further actions are possible."
                )
            else:
                text += f"\n\n*** GAME OVER — VICTORY ***\nYou won a {vtype} victory!"
            try:
                await logger.log_game_over(
                    is_defeat=gameover.is_defeat,
                    winner_civ=gameover.winner_name,
                    winner_leader=gameover.winner_leader,
                    victory_type=vtype,
                    player_alive=gameover.player_alive,
                )
            except Exception:
                log.warning("Failed to log game-over in overview", exc_info=True)
        return text

    return await _logged(ctx, "get_game_overview", {}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_units(ctx: Context) -> str:
    """List all your units with position, type, movement, and health.

    Each unit shows its id and idx (needed for action commands).
    Consumed units (e.g. settlers that founded cities) are excluded.
    """
    gs = _get_game(ctx)

    async def _run():
        units = await gs.get_units()
        try:
            threats = await gs.get_threat_scan()
        except Exception:
            threats = None
        trade_status = None
        try:
            trade_status = await gs.get_trade_routes()
        except Exception:
            pass
        return nr.narrate_units(units, threats, trade_status)

    return await _logged(ctx, "get_units", {}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_spies(ctx: Context) -> str:
    """List all your spy units with position, rank, city, and available missions.

    Shows each spy's composite id (needed for spy_action), current location,
    rank (Recruit/Agent/Special Agent/Senior Agent), XP, and which operations
    are available at their current position.

    Note: offensive missions only become available once the spy has physically
    arrived in the target city. Use spy_action with action='travel' first.
    """
    gs = _get_game(ctx)

    async def _run():
        spies = await gs.get_spies()
        return nr.narrate_spies(spies)

    return await _logged(ctx, "get_spies", {}, _run)


@mcp.tool()
async def spy_action(
    ctx: Context,
    unit_id: int,
    action: Literal[
        "travel",
        "COUNTERSPY",
        "GAIN_SOURCES",
        "SIPHON_FUNDS",
        "STEAL_TECH_BOOST",
        "SABOTAGE_PRODUCTION",
        "GREAT_WORK_HEIST",
        "RECRUIT_PARTISANS",
        "NEUTRALIZE_GOVERNOR",
        "FABRICATE_SCANDAL",
    ],
    target_x: int,
    target_y: int,
) -> str:
    """Send a spy to a city or launch a spy mission.

    Args:
        unit_id: The spy's composite ID (from get_spies output)
        action: 'travel' to move the spy to a city, or a mission type to launch
        target_x: X coordinate of the target city tile
        target_y: Y coordinate of the target city tile

    Travel notes:
        - Valid targets: your own cities and city-states only.
        - Allied civ cities are NOT valid travel targets.
        - Travel is queued end-of-turn; spy position updates after turn ends.

    Mission notes:
        - Spy must be physically IN the target city to launch any offensive mission.
        - Use 'travel' first, then end the turn, then launch the mission.
        - COUNTERSPY defends your own city (spy must be in your city).
        - get_spies shows which ops are available at the spy's current location.
    """
    gs = _get_game(ctx)
    unit_index = unit_id % 65536
    params = {
        "unit_id": unit_id,
        "action": action,
        "target_x": target_x,
        "target_y": target_y,
    }

    async def _run():
        if action == "travel":
            return await gs.spy_travel(unit_index, target_x, target_y)
        return await gs.spy_mission(unit_index, action, target_x, target_y)

    result = await _logged(ctx, "spy_action", params, _run, mutating=True)
    _get_camera(ctx).push(target_x, target_y, f"spy {action}")
    return result


@mcp.tool(annotations={"readOnlyHint": True})
async def get_cities(ctx: Context) -> str:
    """List all your cities with yields, population, production, growth, and loyalty.

    Each city shows its id (needed for production commands).
    Cities losing loyalty show warnings with flip timers.
    """
    gs = _get_game(ctx)

    async def _run():
        cities, distances = await gs.get_cities()
        return nr.narrate_cities(cities, distances)

    return await _logged(ctx, "get_cities", {}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_production_options(ctx: Context, city_id: int) -> str:
    """List what a city can produce right now.

    Args:
        city_id: City ID (from get_cities output)

    Returns available units, buildings, and districts with production costs.
    Call this when a city finishes building or to decide what to produce next.
    """
    gs = _get_game(ctx)

    async def _run():
        options = await gs.list_city_production(city_id)
        return nr.narrate_city_production(options)

    return await _logged(ctx, "get_production_options", {"city_id": city_id}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_map_area(
    ctx: Context, center_x: int, center_y: int, radius: int = 2
) -> str:
    """Get terrain info for tiles around a point.

    Args:
        center_x: X coordinate of center tile
        center_y: Y coordinate of center tile
        radius: How many tiles out from center (default 2, max 4)
    """
    radius = min(radius, 4)
    gs = _get_game(ctx)

    async def _run():
        tiles = await gs.get_map_area(center_x, center_y, radius)
        return nr.narrate_map(tiles)

    result = await _logged(
        ctx,
        "get_map_area",
        {"center_x": center_x, "center_y": center_y, "radius": radius},
        _run,
    )
    _get_camera(ctx).push(center_x, center_y, f"map_area ({center_x},{center_y})")
    return result


@mcp.tool(annotations={"readOnlyHint": True})
async def get_settle_sites_near_unit(ctx: Context, unit_id: int) -> str:
    """List best settle locations near a settler unit.

    Args:
        unit_id: The settler's composite ID (from get_units output)

    Scores locations by yields, water, defense, and resource value.
    Returns top 5 candidates sorted by score.
    """
    gs = _get_game(ctx)
    unit_index = unit_id % 65536
    return await _logged(
        ctx,
        "get_settle_sites_near_unit",
        {"unit_id": unit_id},
        lambda: gs.get_settle_advisor(unit_index),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_pathing_estimate(
    ctx: Context, unit_id: int, target_x: int, target_y: int
) -> str:
    """Estimate how many turns a unit needs to reach a destination.

    Args:
        unit_id: The unit's composite ID (from get_units output)
        target_x: Destination X coordinate
        target_y: Destination Y coordinate

    Returns estimated turns, path length, and reachable tiles this turn.
    """
    gs = _get_game(ctx)
    unit_index = unit_id % 65536

    async def _run():
        est = await gs.get_pathing_estimate(unit_index, target_x, target_y)
        return nr.narrate_pathing_estimate(est)

    return await _logged(
        ctx,
        "get_pathing_estimate",
        {"unit_id": unit_id, "target_x": target_x, "target_y": target_y},
        _run,
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_settle_sites_on_map(ctx: Context) -> str:
    """Find the best settle locations across the entire revealed map.

    Unlike get_settle_sites_near_unit (which searches near a specific settler),
    this scans all revealed land for the top 10 settle candidates.
    Use this when deciding WHERE to send a settler, not just where to settle.
    """
    gs = _get_game(ctx)

    async def _run():
        candidates = await gs.get_global_settle_scan()
        if not candidates:
            return "No valid settle locations found on revealed map."
        return nr.narrate_settle_candidates(candidates)

    return await _logged(ctx, "get_settle_sites_on_map", {}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_builder_tasks(ctx: Context) -> str:
    """Get a prioritized task board for all your builders.

    Scans your territory for tiles needing improvements and matches them
    with idle builders. Like the builder lens in the UI — shows what to
    build where and which builder is closest.

    Priority tiers:
    - URGENT: Pillaged improvements (yield loss), unimproved strategic resources
    - HIGH: Unimproved luxury/bonus resources
    - NORMAL: Empty tiles that could benefit from farms/mines/lumber mills

    Call this before issuing builder orders each turn.
    """
    gs = _get_game(ctx)

    async def _run():
        tasks, builders = await gs.get_builder_tasks()
        return nr.narrate_builder_tasks(tasks, builders)

    return await _logged(ctx, "get_builder_tasks", {}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_empire_resources(ctx: Context) -> str:
    """Get a summary of all resources in and near your empire.

    Shows owned resources (improved/unimproved) grouped by type,
    and unclaimed resources near your cities.
    """
    gs = _get_game(ctx)

    async def _run():
        stockpiles, owned, nearby, luxuries = await gs.get_empire_resources()
        return nr.narrate_empire_resources(stockpiles, owned, nearby, luxuries)

    return await _logged(ctx, "get_empire_resources", {}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_exploration_status(ctx: Context) -> str:
    """Get fog-of-war boundaries and unclaimed resources across the map.

    Shows how far explored territory extends from each city (in 6 directions),
    highlighting directions that need exploration. Also lists unclaimed luxury
    and strategic resources on revealed but unowned land.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_exploration_status",
        {},
        lambda: _narrate(gs.get_strategic_map, nr.narrate_strategic_map),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_diplomacy(ctx: Context) -> str:
    """Get diplomatic status with all known civilizations.

    Shows diplomatic state (Friendly/Neutral/Unfriendly), relationship modifiers
    with scores and reasons, grievances, delegations/embassies, and available
    diplomatic actions you can take. Also shows visible enemy city details
    (name, population, loyalty, walls).
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_diplomacy",
        {},
        lambda: _narrate(gs.get_diplomacy, nr.narrate_diplomacy),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_research_options(ctx: Context) -> str:
    """Get technology and civic research status.

    Shows current research, current civic, turns remaining,
    and lists of available technologies and civics to choose from.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_research_options",
        {},
        lambda: _narrate(gs.get_tech_civics, nr.narrate_tech_civics),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_pending_deals(ctx: Context) -> str:
    """Check for pending trade deal offers from other civilizations.

    Shows what each civ is offering and what they want in return.
    Use respond_to_deal to accept or reject.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_pending_deals",
        {},
        lambda: _narrate(gs.get_pending_deals, nr.narrate_pending_deals),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_policies(ctx: Context) -> str:
    """Get current government, policy slots, and available policies.

    Shows current government type, each policy slot with its type and current
    policy (if any), and all unlocked policies grouped by compatible slot type.
    Wildcard slots accept any policy type.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx, "get_policies", {}, lambda: _narrate(gs.get_policies, nr.narrate_policies)
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_notifications(ctx: Context) -> str:
    """Get all active game notifications.

    Shows action-required items (need your decision) and informational
    notifications. Action-required items include which MCP tool to use
    to resolve them. Call this to check what needs attention without
    ending the turn.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_notifications",
        {},
        lambda: _narrate(gs.get_notifications, nr.narrate_notifications),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_pending_diplomacy(ctx: Context) -> str:
    """Check for pending diplomacy encounters (e.g. first meeting with a civ).

    Diplomacy encounters block turn progression. Call this if end_turn
    reports the turn didn't advance. Returns any open sessions with their
    dialogue text, visible buttons, and response guidance.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_pending_diplomacy",
        {},
        lambda: _narrate(gs.get_diplomacy_sessions, nr.narrate_diplomacy_sessions),
    )


# ---------------------------------------------------------------------------
# Action tools (mutating)
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_governors(ctx: Context) -> str:
    """Get governor status, appointed governors, and available types.

    Shows governor points, currently appointed governors with assignments,
    and governors available to appoint. Use appoint_governor to appoint one.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_governors",
        {},
        lambda: _narrate(gs.get_governors, nr.narrate_governors),
    )


@mcp.tool()
async def appoint_governor(
    ctx: Context, governor_type: str, city_id: Optional[int] = None
) -> str:
    """Appoint a new governor, and optionally assign them to a city.

    Args:
        governor_type: e.g. GOVERNOR_THE_EDUCATOR (Pingala), GOVERNOR_THE_DEFENDER (Victor)
        city_id: City to assign them to (from get_cities output). Omit to
            appoint only and assign later with assign_governor.

    Requires available governor points. Use get_governors to see options.
    An appointed governor takes several turns to establish in their city.
    """
    gs = _get_game(ctx)
    params: dict[str, Any] = {"governor_type": governor_type}
    if city_id is not None:
        params["city_id"] = city_id

    async def _run():
        appointed = await gs.appoint_governor(governor_type)
        if city_id is None:
            return appointed
        if appointed.startswith("Error"):
            return appointed
        assigned = await gs.assign_governor(governor_type, city_id)
        return f"{appointed} | {assigned}"

    return await _logged(ctx, "appoint_governor", params, _run, mutating=True)


@mcp.tool()
async def assign_governor(ctx: Context, governor_type: str, city_id: int) -> str:
    """Assign an appointed governor to a city.

    Args:
        governor_type: The governor type (from get_governors output)
        city_id: The city ID (from get_cities output)

    Governor must already be appointed. Takes several turns to establish.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "assign_governor",
        {"governor_type": governor_type, "city_id": city_id},
        lambda: gs.assign_governor(governor_type, city_id),
        mutating=True,
    )


@mcp.tool()
async def promote_governor(
    ctx: Context, governor_type: str, promotion_type: str
) -> str:
    """Promote a governor with a new ability.

    Args:
        governor_type: The governor type (from get_governors output)
        promotion_type: The promotion type (from get_governors output, shown under each governor)

    Requires available governor points. Use get_governors to see available promotions.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "promote_governor",
        {"governor_type": governor_type, "promotion_type": promotion_type},
        lambda: gs.promote_governor(governor_type, promotion_type),
        mutating=True,
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_unit_promotions(ctx: Context, unit_id: int) -> str:
    """List available promotions for a unit.

    Args:
        unit_id: The unit's composite ID (from get_units output)

    Shows promotions filtered by the unit's promotion class.
    Only units with enough XP will have promotions available.
    """
    gs = _get_game(ctx)

    async def _run():
        status = await gs.get_unit_promotions(unit_id)
        return nr.narrate_unit_promotions(status)

    return await _logged(ctx, "get_unit_promotions", {"unit_id": unit_id}, _run)


@mcp.tool()
async def promote_unit(ctx: Context, unit_id: int, promotion_type: str) -> str:
    """Apply a promotion to a unit.

    Args:
        unit_id: The unit's composite ID (from get_units output)
        promotion_type: e.g. PROMOTION_BATTLECRY, PROMOTION_TORTOISE

    Use get_unit_promotions first to see available options.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "promote_unit",
        {"unit_id": unit_id, "promotion_type": promotion_type},
        lambda: gs.promote_unit(unit_id, promotion_type),
        mutating=True,
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_city_states(ctx: Context) -> str:
    """List known city-states with envoy counts and types.

    Shows envoy tokens available, each city-state's type (Scientific,
    Industrial, etc.), how many envoys you've sent, and who is suzerain.
    Use send_envoy to send an envoy.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_city_states",
        {},
        lambda: _narrate(gs.get_city_states, nr.narrate_city_states),
    )


@mcp.tool()
async def send_envoy(ctx: Context, player_id: int) -> str:
    """Send an envoy to a city-state.

    Args:
        player_id: The city-state's player ID (from get_city_states)

    Requires available envoy tokens. Use get_city_states to see options.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "send_envoy",
        {"player_id": player_id},
        lambda: gs.send_envoy(player_id),
        mutating=True,
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_belief_options(ctx: Context) -> str:
    """Get the beliefs you can choose right now, pantheon or religion.

    Pantheon selection and religion founding are sequential stages, so only
    one of them is ever live. Before you have a pantheon this returns pantheon
    status and available pantheon beliefs; after that it returns religion
    founding status, available religions, and beliefs grouped by class
    (Follower, Founder, Enhancer, Worship). The result names the tool that
    applies — choose_pantheon or found_religion.
    """
    gs = _get_game(ctx)

    async def _run():
        pantheon = await gs.get_pantheon_status()
        if not pantheon.has_pantheon:
            return nr.narrate_pantheon_status(pantheon)
        founding = await gs.get_religion_founding_status()
        return nr.narrate_religion_founding_status(founding)

    return await _logged(ctx, "get_belief_options", {}, _run)


@mcp.tool()
async def choose_pantheon(ctx: Context, belief_type: str) -> str:
    """Found a pantheon with the specified belief.

    Args:
        belief_type: e.g. BELIEF_GOD_OF_THE_FORGE, BELIEF_DIVINE_SPARK

    Use get_belief_options first to see options. Requires enough faith
    and no existing pantheon.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "choose_pantheon",
        {"belief_type": belief_type},
        lambda: gs.choose_pantheon(belief_type),
        mutating=True,
    )


@mcp.tool()
async def found_religion(
    ctx: Context,
    religion_type: str,
    follower_belief_type: str,
    founder_belief_type: str,
) -> str:
    """Found a religion with a chosen name, follower belief, and founder belief.

    Args:
        religion_type: e.g. RELIGION_HINDUISM, RELIGION_BUDDHISM, RELIGION_ISLAM
        follower_belief_type: e.g. BELIEF_WORK_ETHIC, BELIEF_CHORAL_MUSIC
        founder_belief_type: e.g. BELIEF_STEWARDSHIP, BELIEF_CHURCH_PROPERTY

    Requires your Great Prophet to have already activated on a Holy Site
    (via UNITOPERATION_FOUND_RELIGION). Use get_belief_options first to see
    available options.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "found_religion",
        {
            "religion_type": religion_type,
            "follower_belief_type": follower_belief_type,
            "founder_belief_type": founder_belief_type,
        },
        lambda: gs.found_religion(
            religion_type, follower_belief_type, founder_belief_type
        ),
        mutating=True,
    )


@mcp.tool()
async def upgrade_unit(ctx: Context, unit_id: int) -> str:
    """Upgrade a unit to its next type (e.g. Slinger -> Archer).

    Args:
        unit_id: The unit's composite ID (from get_units output)

    Requires the right technology, enough gold, and the unit must have
    moves remaining. The unit's movement is consumed by upgrading.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "upgrade_unit",
        {"unit_id": unit_id},
        lambda: gs.upgrade_unit(unit_id),
        mutating=True,
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_dedications(ctx: Context) -> str:
    """Get current era age, available dedications, and active ones.

    Shows era score thresholds, whether you're in a Golden/Dark/Normal age,
    and lists available dedication choices with their bonuses.
    Use choose_dedication to select one when required.
    """
    gs = _get_game(ctx)

    async def _run():
        status = await gs.get_dedications()
        return nr.narrate_dedications(status)

    return await _logged(ctx, "get_dedications", {}, _run)


@mcp.tool()
async def choose_dedication(ctx: Context, dedication_index: int) -> str:
    """Choose a dedication/commemoration for the current era.

    Args:
        dedication_index: The index of the dedication (from get_dedications output)

    Use get_dedications first to see available options and their bonuses.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "choose_dedication",
        {"dedication_index": dedication_index},
        lambda: gs.choose_dedication(dedication_index),
        mutating=True,
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_deal_options(ctx: Context, player_id: int) -> str:
    """See what both sides can trade — like opening the trade screen.

    Args:
        player_id: The player ID (from get_diplomacy output)

    Shows gold, resources, favor, open borders status, and alliance eligibility
    for both you and the other civilization. Use before propose_deal to see
    what's available.
    """
    gs = _get_game(ctx)

    async def _run():
        opts = await gs.get_deal_options(player_id)
        return nr.narrate_deal_options(opts)

    return await _logged(ctx, "get_deal_options", {"player_id": player_id}, _run)


@mcp.tool()
async def respond_to_deal(ctx: Context, player_id: int, accept: bool) -> str:
    """Accept or reject a pending trade deal.

    Args:
        player_id: The player ID of the civilization (from get_pending_deals)
        accept: True to accept the deal, False to reject it

    Use get_pending_deals first to see what's being offered.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "respond_to_deal",
        {"player_id": player_id, "accept": accept},
        lambda: gs.respond_to_deal(player_id, accept),
        mutating=True,
    )


@mcp.tool()
async def propose_deal(
    ctx: Context,
    player_id: int,
    offer_gold: int = 0,
    offer_gold_per_turn: int = 0,
    offer_resources: list[str] | None = None,
    offer_favor: int = 0,
    offer_open_borders: bool = False,
    request_gold: int = 0,
    request_gold_per_turn: int = 0,
    request_resources: list[str] | None = None,
    request_favor: int = 0,
    request_open_borders: bool = False,
    joint_war_player_id: int = 0,
    mode: Literal["send", "test"] = "send",
) -> str:
    """Propose a trade deal to another civilization.

    Args:
        player_id: The player ID (from get_diplomacy output)
        offer_gold: Lump sum gold to give them
        offer_gold_per_turn: Gold per turn to give them (30-turn duration)
        offer_resources: Resource types to offer, e.g. ["RESOURCE_SILK", "RESOURCE_TEA"]
        offer_favor: Diplomatic favor to offer
        offer_open_borders: True to offer our open borders
        request_gold: Lump sum gold to request from them
        request_gold_per_turn: Gold per turn to request (30-turn duration)
        request_resources: Resource types to request
        request_favor: Diplomatic favor to request from them
        request_open_borders: True to request their open borders
        joint_war_player_id: Player ID of a third civ to declare joint war against
        mode: "send" commits the deal, "test" previews the AI's counter-offer

    Examples: Gift 100 gold: offer_gold=100. Trade silk for 3 gpt:
    offer_resources=["RESOURCE_SILK"], request_gold_per_turn=3.
    Mutual open borders: offer_open_borders=True, request_open_borders=True.
    Test a deal first with mode="test", then commit it with mode="send".
    """
    gs = _get_game(ctx)

    offered_resources = offer_resources if offer_resources is not None else []
    requested_resources = request_resources if request_resources is not None else []

    offer_items: list[dict] = []
    request_items: list[dict] = []
    if offer_gold > 0:
        offer_items.append({"type": "GOLD", "amount": offer_gold, "duration": 0})
    if offer_gold_per_turn > 0:
        offer_items.append(
            {"type": "GOLD", "amount": offer_gold_per_turn, "duration": 30}
        )
    for resource in offered_resources:
        offer_items.append(
            {"type": "RESOURCE", "name": resource, "amount": 1, "duration": 30}
        )
    if offer_favor > 0:
        offer_items.append({"type": "FAVOR", "amount": offer_favor})
    if offer_open_borders:
        offer_items.append({"type": "AGREEMENT", "subtype": "OPEN_BORDERS"})
    if request_gold > 0:
        request_items.append({"type": "GOLD", "amount": request_gold, "duration": 0})
    if request_gold_per_turn > 0:
        request_items.append(
            {"type": "GOLD", "amount": request_gold_per_turn, "duration": 30}
        )
    for resource in requested_resources:
        request_items.append(
            {"type": "RESOURCE", "name": resource, "amount": 1, "duration": 30}
        )
    if request_favor > 0:
        request_items.append({"type": "FAVOR", "amount": request_favor})
    if request_open_borders:
        request_items.append({"type": "AGREEMENT", "subtype": "OPEN_BORDERS"})
    if joint_war_player_id > 0:
        # Joint war is mutual — both sides commit
        offer_items.append({"type": "AGREEMENT", "subtype": "JOINT_WAR"})
        request_items.append({"type": "AGREEMENT", "subtype": "JOINT_WAR"})

    if not offer_items and not request_items:
        return "Error: must specify at least one offer or request item"

    if mode == "test":
        return await _logged(
            ctx,
            "test_deal",
            {
                "player_id": player_id,
                "offer_items": offer_items,
                "request_items": request_items,
            },
            lambda: gs.test_trade(player_id, offer_items, request_items),
            mutating=True,
        )

    return await _logged(
        ctx,
        "propose_deal",
        {
            "player_id": player_id,
            "offer_items": offer_items,
            "request_items": request_items,
        },
        lambda: gs.propose_trade(player_id, offer_items, request_items),
        mutating=True,
    )


@mcp.tool()
async def propose_peace(ctx: Context, player_id: int) -> str:
    """Propose white peace to a civilization you're at war with.

    Args:
        player_id: The player ID (from get_diplomacy output)

    Requires being at war and past the 10-turn war cooldown.
    The AI may accept or reject based on war score and relationship.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "propose_peace",
        {"player_id": player_id},
        lambda: gs.propose_peace(player_id),
        mutating=True,
    )


@mcp.tool()
async def set_policies(ctx: Context, assignments: dict[int, str]) -> str:
    """Set policy cards in government slots.

    Args:
        assignments: Slot index to policy type, e.g.
            {0: "POLICY_AGOGE", 1: "POLICY_URBAN_PLANNING"}
            Slots not listed keep their current policy. Use "NONE" to
            explicitly clear a slot. Use get_policies to see available
            policies and slot indices.

    Wildcard slots can accept any policy type. Military slots accept
    military policies, economic slots accept economic policies, etc.
    """
    if not assignments:
        return "Error: no slot assignments given"

    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "set_policies",
        {"assignments": assignments},
        lambda: gs.set_policies(assignments),
        mutating=True,
    )


@mcp.tool()
async def respond_to_diplomacy(
    ctx: Context, player_id: int, response: Literal["POSITIVE", "NEGATIVE"]
) -> str:
    """Respond to a pending diplomacy encounter.

    Args:
        player_id: The player ID of the other civilization (from get_pending_diplomacy)
        response: POSITIVE is friendly, NEGATIVE is dismissive

    First meetings typically have 2-3 rounds. The tool automatically detects
    and closes goodbye-phase sessions (where dialogue text stops changing).
    If SESSION_CONTINUES is returned, send another response for the next round.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "respond_to_diplomacy",
        {"player_id": player_id, "response": response},
        lambda: gs.diplomacy_respond(player_id, response),
        mutating=True,
    )


@mcp.tool()
async def diplomacy_action(
    ctx: Context,
    player_id: int,
    action: Literal[
        "DIPLOMATIC_DELEGATION",
        "DECLARE_FRIENDSHIP",
        "DENOUNCE",
        "RESIDENT_EMBASSY",
        "OPEN_BORDERS",
        "DECLARE_SURPRISE_WAR",
        "DECLARE_FORMAL_WAR",
        "DECLARE_HOLY_WAR",
        "DECLARE_LIBERATION_WAR",
        "DECLARE_RECONQUEST_WAR",
        "DECLARE_PROTECTORATE_WAR",
        "DECLARE_COLONIAL_WAR",
        "DECLARE_TERRITORIAL_WAR",
    ],
) -> str:
    """Send a proactive diplomatic action to another civilization.

    Args:
        player_id: The player ID (from get_diplomacy output)
        action: The diplomatic action to send

    Delegations cost 25 gold and can be rejected if the civ dislikes you.
    Embassies require Writing tech. Use get_diplomacy to see available actions.
    Surprise war is always available if not allied/friends. Other war types
    (casus belli) require specific civics and conditions.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "diplomacy_action",
        {"player_id": player_id, "action": action},
        lambda: gs.send_diplomatic_action(player_id, action),
        mutating=True,
    )


@mcp.tool()
async def form_alliance(
    ctx: Context,
    player_id: int,
    alliance_type: Literal[
        "MILITARY", "RESEARCH", "CULTURAL", "ECONOMIC", "RELIGIOUS"
    ] = "MILITARY",
) -> str:
    """Form an alliance with another civilization.

    Args:
        player_id: The player ID (from get_diplomacy output)
        alliance_type: The kind of alliance to form

    Requires declared friendship and Diplomatic Service civic.
    Use get_deal_options to check alliance eligibility first.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "form_alliance",
        {"player_id": player_id, "alliance_type": alliance_type},
        lambda: gs.form_alliance(player_id, alliance_type),
        mutating=True,
    )


@mcp.tool()
async def city_attack(
    ctx: Context,
    city_id: int,
    target_x: int,
    target_y: int,
) -> str:
    """Fire a city's ranged attack at a tile.

    Args:
        city_id: City ID (from get_cities output)
        target_x: Target X coordinate
        target_y: Target Y coordinate

    The city must have walls and must not have fired this turn.
    Range is 2 tiles from the city centre.
    """
    gs = _get_game(ctx)
    result = await _logged(
        ctx,
        "city_attack",
        {"city_id": city_id, "target_x": target_x, "target_y": target_y},
        lambda: gs.city_attack(city_id, target_x, target_y),
        mutating=True,
    )
    _get_camera(ctx).push(target_x, target_y, "city attack")
    return result


@mcp.tool()
async def resolve_city_capture(
    ctx: Context,
    action: Literal["keep", "reject", "raze", "liberate_founder", "liberate_previous"],
) -> str:
    """Decide what happens to a city you have just taken.

    Args:
        action: What to do with the city awaiting a decision
            - 'keep': keep it (captured or loyalty-flipped)
            - 'reject': free a disloyal city (loyalty flip only)
            - 'raze': raze a captured city (military conquest only)
            - 'liberate_founder': liberate to its original founder
            - 'liberate_previous': liberate to its previous owner

    Takes no city id: the game holds exactly one city awaiting a decision, and
    that is the city this acts on. Call it when a capture or loyalty flip
    blocks the turn.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "resolve_city_capture",
        {"action": action},
        lambda: gs.resolve_city_capture(action),
        mutating=True,
    )


@mcp.tool()
async def unit_action(
    ctx: Context,
    unit_id: int,
    action: Literal[
        "move",
        "attack",
        "fortify",
        "skip",
        "found_city",
        "improve",
        "repair",
        "remove_improvement",
        "remove_feature",
        "build_route",
        "automate",
        "heal",
        "alert",
        "sleep",
        "delete",
        "trade_route",
        "activate",
        "sacrifice_charges",
        "teleport",
        "spread_religion",
    ],
    target_x: Optional[int] = None,
    target_y: Optional[int] = None,
    improvement_type: Optional[str] = None,
) -> str:
    """Issue a command to a unit.

    Args:
        unit_id: The unit's composite ID (from get_units output)
        action: The command to issue
        target_x: Target X coordinate (required for move/attack/trade_route/teleport)
        target_y: Target Y coordinate (required for move/attack/trade_route/teleport)
        improvement_type: Improvement type for builders (required for improve), e.g.
            IMPROVEMENT_FARM. get_units lists what each builder can build here.

    Verbs that act at the unit's current tile: improve, repair,
    remove_improvement, remove_feature, build_route, activate,
    sacrifice_charges, spread_religion, found_city.

    teleport: destination city tile. Traders only, must be idle (not on an active route).
    repair: repairs a pillaged improvement. No improvement name needed.
    remove_improvement: demolishes an intact improvement (e.g. to replace a farm
        with a mine). Costs one charge.
    activate: activates a Great Person on their matching district.
    sacrifice_charges: Royal Society builder sacrifice — spends ALL builder
        charges to boost a district project (2% of cost per charge). Builder
        must be on the district tile.
    spread_religion: Missionaries/Apostles only.
    build_route: builds road/railroad. Military Engineers only. No charges used;
        costs 1 Iron + 1 Coal per railroad tile.
    heal: fortify until healed (auto-wake at full HP).
    alert: sleep but auto-wake when an enemy enters sight range.
    delete: permanently disband the unit.
    """
    gs = _get_game(ctx)
    unit_index = unit_id % 65536
    params: dict[str, Any] = {"unit_id": unit_id, "action": action}
    if target_x is not None:
        params["target_x"] = target_x
    if target_y is not None:
        params["target_y"] = target_y
    if improvement_type:
        params["improvement_type"] = improvement_type

    async def _run():
        match action:
            case "move":
                if target_x is None or target_y is None:
                    return "Error: move requires target_x and target_y"
                return await gs.move_unit(unit_index, target_x, target_y)
            case "attack":
                if target_x is None or target_y is None:
                    return "Error: attack requires target_x and target_y"
                return await gs.attack_unit(unit_index, target_x, target_y)
            case "fortify":
                return await gs.fortify_unit(unit_index)
            case "skip":
                return await gs.skip_unit(unit_index)
            case "found_city":
                return await gs.found_city(unit_index)
            case "improve":
                if not improvement_type:
                    return "Error: improve requires improvement_type (e.g. IMPROVEMENT_FARM). To repair a pillaged improvement, use action='repair' instead."
                return await gs.improve_tile(unit_index, improvement_type)
            case "repair":
                return await gs.repair_improvement(unit_index)
            case "remove_improvement":
                return await gs.remove_improvement(unit_index)
            case "remove_feature":
                return await gs.remove_feature(unit_index)
            case "build_route":
                return await gs.build_route(unit_index)
            case "automate":
                return await gs.automate_explore(unit_index)
            case "heal":
                return await gs.heal_unit(unit_index)
            case "alert":
                return await gs.alert_unit(unit_index)
            case "sleep":
                return await gs.sleep_unit(unit_index)
            case "delete":
                return await gs.delete_unit(unit_index)
            case "trade_route":
                if target_x is None or target_y is None:
                    return "Error: trade_route requires target_x and target_y of destination city"
                return await gs.make_trade_route(unit_index, target_x, target_y)
            case "activate":
                return await gs.activate_great_person(unit_index)
            case "sacrifice_charges":
                return await gs.sacrifice_builder_charges(unit_index)
            case "spread_religion":
                return await gs.spread_religion(unit_index)
            case "teleport":
                if target_x is None or target_y is None:
                    return "Error: teleport requires target_x and target_y of the destination city"
                return await gs.teleport_to_city(unit_index, target_x, target_y)
            case _:
                return f"Error: Unknown action '{action}'"

    result = await _logged(ctx, "unit_action", params, _run, mutating=True)
    if (
        action in ("move", "attack", "trade_route", "teleport")
        and target_x is not None
        and target_y is not None
    ):
        _get_camera(ctx).push(target_x, target_y, f"{action}→({target_x},{target_y})")
    return result


@mcp.tool()
async def skip_remaining_units(ctx: Context) -> str:
    """Skip all units that still have moves remaining.

    Useful after diplomacy encounters invalidate all standing orders.
    Uses GameCore FinishMoves on each unit — fast, reliable, no async issues.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "skip_remaining_units",
        {},
        lambda: gs.skip_remaining_units(),
        mutating=True,
    )


@mcp.tool()
async def set_city_production(
    ctx: Context,
    city_id: int,
    item_type: str,
    target_x: int | None = None,
    target_y: int | None = None,
) -> str:
    """Set what a city should produce.

    Args:
        city_id: City ID (from get_cities output)
        item_type: e.g. UNIT_WARRIOR, BUILDING_MONUMENT, DISTRICT_CAMPUS, PROJECT_LAUNCH_EARTH_SATELLITE
        target_x: X coordinate for district/wonder placement (required for districts — use get_district_sites to find best tile)
        target_y: Y coordinate for district/wonder placement

    Tip: call get_cities first to see your cities and their IDs.
    """
    category = _category_from_prefix(item_type, PRODUCIBLE_CATEGORIES)
    if category is None:
        return _unknown_prefix_error(item_type, PRODUCIBLE_CATEGORIES)

    gs = _get_game(ctx)
    params: dict = {"city_id": city_id, "item_type": item_type}
    if target_x is not None:
        params["target_x"] = target_x
        params["target_y"] = target_y
    return await _logged(
        ctx,
        "set_city_production",
        params,
        lambda: gs.set_city_production(
            city_id, category, item_type, target_x, target_y
        ),
        mutating=True,
    )


@mcp.tool()
async def purchase_item(
    ctx: Context,
    city_id: int,
    item_type: str,
    yield_type: Literal["YIELD_GOLD", "YIELD_FAITH"] = "YIELD_GOLD",
) -> str:
    """Purchase a unit or building instantly with gold or faith.

    Args:
        city_id: City ID (from get_cities output)
        item_type: e.g. UNIT_WARRIOR, BUILDING_MONUMENT
        yield_type: What to spend

    Costs gold/faith immediately. Use get_production_options to see what's available.
    """
    category = _category_from_prefix(item_type, PURCHASABLE_CATEGORIES)
    if category is None:
        return _unknown_prefix_error(item_type, PURCHASABLE_CATEGORIES)

    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "purchase_item",
        {
            "city_id": city_id,
            "item_type": item_type,
            "yield_type": yield_type,
        },
        lambda: gs.purchase_item(city_id, category, item_type, yield_type),
        mutating=True,
    )


@mcp.tool()
async def set_tech(ctx: Context, tech_type: str) -> str:
    """Choose a technology to research in the science tree.

    Args:
        tech_type: The type name, e.g. TECH_POTTERY, from get_research_options

    The two trees research in parallel; set_civic drives the culture tree and
    does not disturb this one.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "set_tech",
        {"tech_type": tech_type},
        lambda: gs.set_research(tech_type),
        mutating=True,
    )


@mcp.tool()
async def set_civic(ctx: Context, civic_type: str) -> str:
    """Choose a civic to research in the culture tree.

    Args:
        civic_type: The type name, e.g. CIVIC_CRAFTSMANSHIP, from get_research_options

    The two trees research in parallel; set_tech drives the science tree and
    does not disturb this one.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "set_civic",
        {"civic_type": civic_type},
        lambda: gs.set_civic(civic_type),
        mutating=True,
    )


@mcp.tool(annotations={"destructiveHint": True})
async def end_turn(
    ctx: Context,
) -> str:
    """End the current turn.

    Make sure you've moved all units, set production, and chosen research
    before ending the turn.

    Returns the turn result along with empire warnings and victory-proximity
    alerts. If a blocker is reported (unmoved units, empty production queue,
    pending research or policy choice), resolve it and call end_turn again.
    """
    gs = _get_game(ctx)

    # Keep the logger's turn counter fresh before advancing. Connection-loss
    # recovery in _logged(, mutating=True) reads it to pick the autosave to restart from,
    # and the agent may not call get_game_overview every turn. The turn
    # number also feeds the World Congress blocker safety net below.
    current_turn = 0
    try:
        ov = await gs.get_game_overview()
        current_turn = ov.turn
        _get_logger(ctx).set_turn(ov.turn)
    except Exception:
        log.warning("end_turn: failed to sync turn counter", exc_info=True)

    # Advance the turn
    result = await _logged(ctx, "end_turn", {}, gs.end_turn, mutating=True)

    # ---------------------------------------------------------------
    # Auto-recover from AI turn hangs (transparent to agent).
    # end_turn returns "HANG:{turn}:{save}|..." when AI processing is
    # stuck after ~39s of polling with no blockers found.
    # Recovery: restart_game from the MCP autosave, reconnect, retry
    # up to _MAX_HANG_RETRIES times with escalating waits.
    # ---------------------------------------------------------------
    _MAX_HANG_RETRIES = 3
    _HANG_EXTRA_WAIT = [0, 15, 30]  # extra seconds before retry per attempt

    if result.startswith("HANG:") and not gs._hang_retry_active:
        parts = result.split("|", 1)
        hang_info = parts[
            0
        ]  # "HANG:57:AutoSave_0057" (Linux) or "HANG:57:0_MCP_0057" (Windows)
        _, hang_turn, hang_save = hang_info.split(":")
        hang_turn_int = int(hang_turn)

        # Check save file exists before attempting recovery.
        # MCP saves (0_MCP_*) are in SINGLE_SAVE_DIR; game autosaves
        # (AutoSave_*) are in SAVE_DIR (auto/ subdir).
        save_path = os.path.join(game_launcher.SINGLE_SAVE_DIR, f"{hang_save}.Civ6Save")
        if not os.path.exists(save_path):
            save_path = os.path.join(game_launcher.SAVE_DIR, f"{hang_save}.Civ6Save")
        if not os.path.exists(save_path):
            log.error(
                "HANG RECOVERY: Save file %s not found, cannot auto-recover",
                save_path,
            )
            # Fall through — return the hang message to agent
        else:
            identity_before = gs._game_identity
            gs._hang_retry_active = True
            try:
                for attempt in range(1, _MAX_HANG_RETRIES + 1):
                    extra_wait = _HANG_EXTRA_WAIT[
                        min(attempt - 1, len(_HANG_EXTRA_WAIT) - 1)
                    ]
                    log.warning(
                        "HANG RECOVERY: attempt %d/%d for T%s "
                        "(extra wait: %ds, save: %s)",
                        attempt,
                        _MAX_HANG_RETRIES,
                        hang_turn,
                        extra_wait,
                        hang_save,
                    )

                    # Step 1: Kill + relaunch + OCR load
                    restart_result = await game_launcher.restart_and_load(hang_save)
                    log.info("HANG RECOVERY: restart: %s", restart_result)

                    # Step 2: Reconnect
                    conn = gs.conn
                    reconnected = False
                    for rc_attempt in range(30):
                        try:
                            await conn.reconnect()
                            if conn.gamecore_index is not None:
                                reconnected = True
                                break
                        except ConnectionError:
                            pass
                        await asyncio.sleep(1)

                    if not reconnected:
                        log.error(
                            "HANG RECOVERY: could not reconnect (attempt %d)",
                            attempt,
                        )
                        continue  # try the whole cycle again

                    # Step 2b: Verify correct game loaded (with retries).
                    # The game may still be on the leader screen after
                    # restart — Lua states exist but game APIs aren't
                    # fully initialized. Retry the check rather than
                    # restarting the entire recovery cycle.
                    if identity_before is not None:
                        identity_ok = False
                        for id_check in range(3):
                            try:
                                actual = await gs.get_game_identity()
                                if actual == identity_before:
                                    identity_ok = True
                                    break
                                log.warning(
                                    "HANG RECOVERY: wrong identity %s vs %s "
                                    "(check %d/3)",
                                    actual,
                                    identity_before,
                                    id_check + 1,
                                )
                            except Exception:
                                log.debug(
                                    "HANG RECOVERY: identity check failed "
                                    "(check %d/3), waiting...",
                                    id_check + 1,
                                )
                            await asyncio.sleep(5)
                        if not identity_ok:
                            log.warning(
                                "HANG RECOVERY: identity check inconclusive "
                                "— proceeding anyway (attempt %d)",
                                attempt,
                            )

                    # Step 3: Reset state flags
                    gs._pending_end_turn = False
                    gs._pending_end_turn_from = None

                    # Step 4: Extra wait to give AI more processing time
                    if extra_wait > 0:
                        log.info(
                            "HANG RECOVERY: waiting %ds before retry...",
                            extra_wait,
                        )
                        await asyncio.sleep(extra_wait)

                    # Step 5: Retry end_turn
                    log.info(
                        "HANG RECOVERY: retrying end_turn for T%s...",
                        hang_turn,
                    )
                    result = await gs.end_turn()
                    log.info("HANG RECOVERY: retry result: %s", result[:200])

                    if not result.startswith("HANG:"):
                        log.info(
                            "HANG RECOVERY: T%s resolved on attempt %d",
                            hang_turn,
                            attempt,
                        )
                        break  # success — fall through to normal processing
                else:
                    # All retries exhausted
                    earlier = max(1, hang_turn_int - 3)
                    log.error(
                        "HANG RECOVERY: all %d attempts failed for T%s",
                        _MAX_HANG_RETRIES,
                        hang_turn,
                    )
                    return (
                        f"AI turn hung at T{hang_turn} after "
                        f"{_MAX_HANG_RETRIES} automatic restart attempts "
                        f"with escalating waits. The hang may be "
                        f"probabilistic — another attempt could work. "
                        f"Try restart_game('{hang_save.replace(hang_turn, str(earlier))}') "
                        f"to skip back a few turns."
                    )
            except Exception:
                log.error("HANG RECOVERY: failed", exc_info=True)
                return (
                    f"HANG RECOVERY FAILED at T{hang_turn}: "
                    f"the restart threw an exception. "
                    f"Try restart_game('{hang_save}') manually."
                )
            finally:
                gs._hang_retry_active = False

    # Clear stale camera events on successful turn advance
    turn_advanced = (
        "->" in result and "Cannot end turn" not in result and "Error" not in result
    )
    if turn_advanced:
        _get_camera(ctx).clear()
        # Update logger turn from result ("Turn X -> Y")
        m = re.search(r"Turn \d+ -> (\d+)", result)
        if m:
            _get_logger(ctx).set_turn(int(m.group(1)))
    elif "Turn paused" in result or "World Congress fires" in result:
        # Safety net: if WC blocker fires repeatedly on the same turn,
        # auto-submit to break infinite loops (agent used wrong voting tool)
        if "World Congress fires" in result:
            wc_turn = getattr(gs, "_wc_blocker_turn", -1)
            wc_count = getattr(gs, "_wc_blocker_count", 0)
            current = current_turn or 0
            if wc_turn == current:
                gs._wc_blocker_count = wc_count + 1
                if gs._wc_blocker_count >= 3:
                    log.warning(
                        "WC blocker repeated %d times on T%d — auto-submitting",
                        gs._wc_blocker_count,
                        current,
                    )
                    try:
                        await gs.submit_congress()
                    except Exception:
                        log.debug("WC auto-submit failed", exc_info=True)
            else:
                gs._wc_blocker_turn = current
                gs._wc_blocker_count = 1

    # Log structured game-over entry.
    # Also check on HANG — the game may have ended during AI processing but
    # InGame Lua froze, so end_turn returned HANG instead of GAME OVER.
    # The GameCore fallback in check_game_over can detect this.
    if "HANG:" in result and "GAME OVER" not in result:
        try:
            hang_check = await gs.check_game_over()
            if hang_check is not None:
                gs._last_game_over = hang_check
                vtype = (
                    hang_check.victory_type.replace("VICTORY_", "")
                    .replace("_", " ")
                    .title()
                )
                if hang_check.is_defeat:
                    result = (
                        f"GAME OVER — DEFEAT. {hang_check.winner_leader} "
                        f"of {hang_check.winner_name} won a {vtype} victory. "
                        f"The game has ended. No further actions are possible."
                    )
                else:
                    result = (
                        f"GAME OVER — VICTORY! You won a {vtype} victory! "
                        f"The game has ended."
                    )
        except Exception:
            log.debug("HANG game-over recheck failed", exc_info=True)

    if "GAME OVER" in result:
        try:
            gameover = gs._last_game_over
            if gameover is None:
                gameover = await gs.check_game_over()
            if gameover is not None:
                gs._last_game_over = None
                vtype = (
                    gameover.victory_type.replace("VICTORY_", "")
                    .replace("_", " ")
                    .title()
                )
                await _get_logger(ctx).log_game_over(
                    is_defeat=gameover.is_defeat,
                    winner_civ=gameover.winner_name,
                    winner_leader=gameover.winner_leader,
                    victory_type=vtype,
                    player_alive=gameover.player_alive,
                )
            else:
                log.error(
                    "GAME OVER detected but no GameOverStatus available "
                    "— outcome will be missing from log"
                )
        except Exception:
            log.warning("Failed to log game-over entry", exc_info=True)

    return result


# ---------------------------------------------------------------------------
# Trade routes
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_trade_routes(ctx: Context) -> str:
    """Get trade route capacity, active routes, and trader status.

    Shows how many routes are active vs capacity, and lists all trader
    units with their positions and whether they're idle or on a route.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "get_trade_routes",
        {},
        lambda: _narrate(gs.get_trade_routes, nr.narrate_trade_routes),
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_trade_destinations(ctx: Context, unit_id: int) -> str:
    """List valid trade route destinations for a trader unit.

    Args:
        unit_id: The trader's composite ID (from get_units output)

    Shows domestic and international destinations. Use unit_action
    with action='trade_route' and target_x/target_y to start a route.
    """
    gs = _get_game(ctx)
    unit_index = unit_id % 65536

    async def _run():
        dests = await gs.get_trade_destinations(unit_index)
        return nr.narrate_trade_destinations(dests)

    return await _logged(ctx, "get_trade_destinations", {"unit_id": unit_id}, _run)


# ---------------------------------------------------------------------------
# District advisor
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_district_sites(ctx: Context, city_id: int, district_type: str) -> str:
    """Show best tiles to place a district with adjacency bonuses.

    Args:
        city_id: City ID (from get_cities)
        district_type: e.g. DISTRICT_CAMPUS, DISTRICT_HOLY_SITE, DISTRICT_INDUSTRIAL_ZONE

    Returns valid placement tiles ranked by adjacency bonus.
    Use set_city_production with target_x/target_y to build the district.
    """
    gs = _get_game(ctx)

    async def _run():
        result = await gs.get_district_advisor(city_id, district_type)
        if isinstance(result, str):
            return f"Error: {result}"  # propagate specific error reason
        narrated = nr.narrate_district_advisor(result, district_type)
        if gs._advisor_budget_warning:
            warn = gs._advisor_budget_warning
            gs._advisor_budget_warning = None
            return f"!! {warn}\n\n{narrated}"
        return narrated

    return await _logged(
        ctx,
        "get_district_sites",
        {"city_id": city_id, "district_type": district_type},
        _run,
    )


@mcp.tool(annotations={"readOnlyHint": True})
async def get_wonder_sites(ctx: Context, city_id: int, wonder_type: str) -> str:
    """Show best tiles to place a wonder with displacement cost analysis.

    Args:
        city_id: City ID (from get_cities output)
        wonder_type: Wonder building type, e.g. BUILDING_CHICHEN_ITZA, BUILDING_ORSZAGHAZ

    Returns valid placement tiles ranked by displacement cost (lowest = best):
    tiles with no improvements or resources are preferred over productive tiles.
    Also shows terrain, feature, river/coastal status, and any resources/improvements
    that would be removed by placing the wonder there.
    Use set_city_production with target_x/target_y to build the wonder.
    """
    gs = _get_game(ctx)

    async def _run():
        placements = await gs.get_wonder_advisor(city_id, wonder_type)
        if isinstance(placements, str):
            return f"Error: {placements}"  # propagate budget/error string
        narrated = nr.narrate_wonder_advisor(placements, wonder_type)
        if gs._advisor_budget_warning:
            warn = gs._advisor_budget_warning
            gs._advisor_budget_warning = None
            return f"!! {warn}\n\n{narrated}"
        return narrated

    return await _logged(
        ctx,
        "get_wonder_sites",
        {"city_id": city_id, "wonder_type": wonder_type},
        _run,
    )


# ---------------------------------------------------------------------------
# Tile purchase tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_purchasable_tiles(ctx: Context, city_id: int) -> str:
    """List tiles a city can purchase with gold.

    Args:
        city_id: City ID (from get_cities)

    Shows cost, terrain, and resources for each purchasable tile.
    Tiles with luxury/strategic resources are listed first.
    """
    gs = _get_game(ctx)

    async def _run():
        tiles = await gs.get_purchasable_tiles(city_id)
        return nr.narrate_purchasable_tiles(tiles)

    return await _logged(ctx, "get_purchasable_tiles", {"city_id": city_id}, _run)


@mcp.tool()
async def purchase_tile(
    ctx: Context, city_id: int, target_x: int, target_y: int
) -> str:
    """Buy a tile for a city with gold.

    Args:
        city_id: City ID
        target_x: Tile X coordinate
        target_y: Tile Y coordinate

    Use get_purchasable_tiles first to see costs and options.
    """
    gs = _get_game(ctx)
    result = await _logged(
        ctx,
        "purchase_tile",
        {"city_id": city_id, "target_x": target_x, "target_y": target_y},
        lambda: gs.purchase_tile(city_id, target_x, target_y),
        mutating=True,
    )
    _get_camera(ctx).push(target_x, target_y, f"purchase tile ({target_x},{target_y})")
    return result


# ---------------------------------------------------------------------------
# Government change
# ---------------------------------------------------------------------------


@mcp.tool()
async def set_government(ctx: Context, government_type: str) -> str:
    """Switch to a different government type.

    Args:
        government_type: e.g. GOVERNMENT_CLASSICAL_REPUBLIC, GOVERNMENT_OLIGARCHY

    Use get_policies to see current government. First switch after
    unlocking a new tier is free (no anarchy).
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "set_government",
        {"government_type": government_type},
        lambda: gs.change_government(government_type),
        mutating=True,
    )


# ---------------------------------------------------------------------------
# Great People
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_great_people(ctx: Context) -> str:
    """See available Great People and recruitment progress.

    Shows which Great People are available, their recruitment cost,
    and which civilization (if any) is recruiting them.
    """
    gs = _get_game(ctx)

    async def _run():
        gp = await gs.get_great_people()
        return nr.narrate_great_people(gp)

    return await _logged(ctx, "get_great_people", {}, _run)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_great_person_sites(ctx: Context, unit_id: int) -> str:
    """Show best cities to activate a Great Person, ranked by suitability.

    Args:
        unit_id: The Great Person unit's composite ID (from get_units output)

    Lists all cities with the matching district (e.g., campuses for Great Scientists),
    showing which ones the GP can activate on, distance, city yield, and great work
    slot availability for cultural GPs.
    """
    gs = _get_game(ctx)
    unit_index = unit_id % 65536

    async def _run():
        result = await gs.get_gp_advisor(unit_index)
        if result is None:
            return "Could not get GP advisor info. Is this a Great Person unit?"
        return nr.narrate_gp_advisor(result)

    return await _logged(ctx, "get_great_person_sites", {"unit_id": unit_id}, _run)


@mcp.tool()
async def great_person_action(
    ctx: Context,
    individual_id: int,
    action: Literal["recruit", "patronize", "reject"],
    yield_type: Literal["YIELD_GOLD", "YIELD_FAITH"] = "YIELD_GOLD",
) -> str:
    """Recruit, buy, or pass on a Great Person candidate.

    Args:
        individual_id: The individual's ID (from get_great_people output, shown after ability)
        action: recruit spends accumulated Great Person points; patronize buys
            the candidate instantly; reject passes to the next candidate in
            that class
        yield_type: What patronize spends. Ignored by recruit and reject.

    recruit: requires enough Great Person points for that class. The GP spawns
        in your capital. get_great_people marks candidates [CAN RECRUIT].
    patronize: costs are shown in get_great_people output under "Patronize:".
    reject: costs faith, and makes the next Great Person of that class available.
    """
    gs = _get_game(ctx)
    params: dict[str, Any] = {"individual_id": individual_id, "action": action}
    if action == "patronize":
        params["yield_type"] = yield_type

    async def _run():
        match action:
            case "recruit":
                return await gs.recruit_great_person(individual_id)
            case "patronize":
                return await gs.patronize_great_person(individual_id, yield_type)
            case "reject":
                return await gs.reject_great_person(individual_id)
            case _:
                return f"Error: Unknown great person action '{action}'"

    return await _logged(ctx, "great_person_action", params, _run, mutating=True)


# ---------------------------------------------------------------------------
# World Congress
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_world_congress(ctx: Context) -> str:
    """Get World Congress status, active resolutions, and voting options.

    Shows whether congress is in session, resolutions to vote on (with options A/B
    and possible targets), turns until next session, and your diplomatic favor.
    When in session, use queue_world_congress_votes to register votes before end_turn.
    """
    gs = _get_game(ctx)

    async def _run():
        status = await gs.get_world_congress()
        return nr.narrate_world_congress(status)

    return await _logged(ctx, "get_world_congress", {}, _run)


class WorldCongressVote(BaseModel):
    """One resolution's voting preference."""

    hash: int = Field(description="Resolution type hash, from get_world_congress")
    option: Literal[1, 2] = Field(description="1 for option A, 2 for option B")
    target: int = Field(
        description="Player ID for PlayerType resolutions, otherwise the raw "
        "target value; resolved to a 0-based index at runtime"
    )
    votes: int = Field(description="Maximum votes to allocate, as favor allows")


@mcp.tool()
async def queue_world_congress_votes(
    ctx: Context, votes: list[WorldCongressVote]
) -> str:
    """Pre-configure World Congress votes for the upcoming session.

    Args:
        votes: One entry per resolution you want to vote on.

    Call this BEFORE end_turn when get_world_congress shows 0 turns until next
    session. Registers an event handler that fires during WC processing and
    casts your votes with the specified preferences.

    If you don't call this, end_turn will pause at the World Congress session
    and return control to you for interactive voting.
    """
    gs = _get_game(ctx)
    vote_list: list[dict] = []
    for vote in votes:
        vote_list.append(vote.model_dump())

    return await _logged(
        ctx,
        "queue_world_congress_votes",
        {"votes": vote_list},
        lambda: gs.queue_wc_votes(vote_list),
        mutating=True,
    )


# ---------------------------------------------------------------------------
# Victory progress
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_victory_progress(ctx: Context) -> str:
    """Get victory condition progress for all civilizations.

    Shows progress toward Science, Domination, Culture, Religious,
    Diplomatic, and Score victories. Includes space race VP, diplomatic VP,
    tourism vs domestic tourists, religion spread, capital ownership,
    and military strength. Call every 20-30 turns to track the race.
    """
    gs = _get_game(ctx)

    async def _run():
        vp = await gs.get_victory_progress()
        return nr.narrate_victory_progress(vp)

    return await _logged(ctx, "get_victory_progress", {}, _run)


# ---------------------------------------------------------------------------
# Religion status
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_religion_spread(ctx: Context) -> str:
    """Get per-city religion breakdown across all visible cities.

    Shows which religion is majority in each city, follower counts,
    and which religions are closest to religious victory.
    """
    gs = _get_game(ctx)

    async def _run():
        rs = await gs.get_religion_status()
        return nr.narrate_religion_status(rs)

    return await _logged(ctx, "get_religion_spread", {}, _run)


# ---------------------------------------------------------------------------
# City yield focus
# ---------------------------------------------------------------------------


@mcp.tool()
async def set_city_focus(
    ctx: Context,
    city_id: int,
    focus: Literal[
        "food", "production", "gold", "science", "culture", "faith", "default"
    ],
) -> str:
    """Set a city's citizen yield priority.

    Args:
        city_id: City ID
        focus: The yield to prioritise. 'default' clears all focus settings.

    Cities automatically assign citizens to tiles. This biases the AI
    toward the chosen yield type when assigning new citizens.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "set_city_focus",
        {"city_id": city_id, "focus": focus},
        lambda: gs.set_city_focus(city_id, focus),
        mutating=True,
    )


# ---------------------------------------------------------------------------
# Utility tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"destructiveHint": True})
async def run_lua(
    ctx: Context, code: str, context: Literal["gamecore", "ingame"] = "gamecore"
) -> str:
    """Run arbitrary Lua code in the game. Advanced escape hatch — prefer built-in tools.

    Args:
        code: Lua code to execute. Use print() for output, end with print("---END---").
        context: Which Lua state to run in.

    Context differences:
      gamecore: Players[], GameInfo.*, Map.*, Game.* — safe read-only access.
                CANNOT use: UI.*, UnitManager.*, CityManager.*, notifications.
      ingame:   All APIs including UI.*, UnitManager.*, CityManager.*.
                Use for: moving units, setting research, diplomacy actions.

    Always use print() for output (not return).
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "run_lua",
        {"context": context},
        lambda: gs.execute_lua(code, context),
        mutating=True,
    )


# ---------------------------------------------------------------------------
# Save / Load
# ---------------------------------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True})
async def get_saves(ctx: Context) -> str:
    """List available save files (normal, autosave).

    Returns the save names. Use load_game(save_name=...) to load one.
    """
    gs = _get_game(ctx)
    return await _logged(ctx, "get_saves", {}, gs.list_saves)


@mcp.tool(annotations={"destructiveHint": True})
async def load_game(ctx: Context, save_name: str) -> str:
    """Load a save file by name. No need to call get_saves first.

    Args:
        save_name: Save name without extension (e.g. "0_MCP_0079",
                   "0A_GROUND_CONTROL", "AutoSave_0221", "quicksave").

    Tries Lua-based loading first (fast, ~5s). If the save isn't found
    via Lua (common for autosaves/quicksaves), falls back to OCR menu
    navigation (~90s) after verifying the file exists on disk.

    The game reloads entirely. Wait ~10 seconds after calling this, then use
    get_game_overview to verify the loaded state.
    """
    gs = _get_game(ctx)
    return await _logged(
        ctx,
        "load_game",
        {"save_name": save_name},
        lambda: gs.load_game_save(save_name),
    )


@mcp.tool(annotations={"destructiveHint": True})
async def restart_game(ctx: Context, save_name: str | None = None) -> str:
    """Full game recovery: kill, relaunch, and load a save.

    Args:
        save_name: Autosave name (e.g. "AutoSave_0221"). If not provided,
                   loads the most recent autosave.

    This is the recommended tool for recovering from game hangs (e.g. AI turn
    processing stuck in infinite loop). Takes 60-120 seconds total:
    1. Kills the game process
    2. Waits for Steam to deregister (~10s)
    3. Relaunches via Steam (~15-30s for process start + main menu)
    4. Navigates menus via OCR to load the save (~30-60s)

    After completion, wait ~10 seconds then call get_game_overview to verify.
    """
    gs = _get_game(ctx)
    identity_before = gs._game_identity

    result = await game_launcher.restart_and_load(save_name)

    # Reconnect and verify correct game loaded
    conn = gs.conn
    for attempt in range(30):
        try:
            await conn.reconnect()
            if conn.gamecore_index is not None:
                break
        except ConnectionError:
            pass
        await asyncio.sleep(1)

    if conn.gamecore_index is not None and identity_before is not None:
        try:
            actual = await gs.get_game_identity()
            if actual != identity_before:
                log.warning(
                    "restart_game: wrong game loaded (expected %s, got %s) — retrying",
                    identity_before,
                    actual,
                )
                result2 = await game_launcher.restart_and_load(save_name)
                for attempt in range(30):
                    try:
                        await conn.reconnect()
                        if conn.gamecore_index is not None:
                            break
                    except ConnectionError:
                        pass
                    await asyncio.sleep(1)
                try:
                    actual2 = await gs.get_game_identity()
                    if actual2 != identity_before:
                        return (
                            f"{result2} | WARNING: Wrong game loaded "
                            f"(expected {identity_before[0]}, "
                            f"got {actual2[0]}). Manual recovery needed."
                        )
                except Exception:
                    pass
                return f"{result2} | Reloaded after wrong-game detection."
        except Exception:
            log.debug("Post-load identity check failed", exc_info=True)

    return result


async def _narrate(
    query_fn: Callable[[], Awaitable[Any]], narrate_fn: Callable[..., str]
) -> str:
    """Helper: call a query function then narrate the result."""
    data = await query_fn()
    return narrate_fn(data)


def main():
    """Entry point for the MCP server."""
    import signal

    logging.basicConfig(level=logging.INFO)

    # Remap SIGTERM → SIGINT so asyncio's existing SIGINT handler triggers a
    # graceful shutdown (cancels all tasks → lifespan finally block runs →
    # conn.disconnect() closes the FireTuner TCP connection cleanly).
    # Without this, SIGTERM kills the process immediately, leaving the game
    # with an abrupt TCP RST which can cause it to crash.
    # SIGTERM is not available on Windows, so skip the remap there.
    if hasattr(signal, "SIGTERM"):
        signal.signal(
            signal.SIGTERM, lambda sig, frame: os.kill(os.getpid(), signal.SIGINT)
        )

    if os.environ.get("CIV_MCP_DISABLE_LUA"):
        mcp._tool_manager.remove_tool("run_lua")

    mcp.run(transport="stdio")
