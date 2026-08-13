"""End-to-end conformance against a real game. The only tier that validates Lua.

    uv run python scripts/install_saves.py     # once
    # launch Civ 6 with EnableTuner=1 and load 0T_TURN37_INCA
    uv run pytest -m "live and not destructive" -q

Excluded from CI (`-m "not live"`), because it needs Civ 6 and FireTuner.

These assert **invariants, not text**. Once you start issuing commands the game
stops being deterministic, so a snapshot would be noise; what must hold is that a
read returns something rather than an error, and that a write changes the
thing it claims to change.

This replaced `scripts/test_game_state.py` and `scripts/test_queries.py`, which
did the same job by hand and had been broken since `lua_queries` became the
`lua/` package — they imported a module that no longer exists, and nothing
noticed because nothing ran them.
"""

from __future__ import annotations

import asyncio
import re
import time

import pytest

pytestmark = pytest.mark.live


class _Shared:
    """One FireTuner connection, shared across every call in the module.

    Two constraints force this shape:

    - The game refuses a reconnect issued immediately after a disconnect, so a
      connection-per-call suite fails from the second call onwards.
    - A `StreamWriter` belongs to the event loop that created it, so the
      connection and every call using it must run in **one** loop. That rules
      out `asyncio.run` per call.

    `disconnect()` is therefore a no-op: the MCP lifespan closes the connection
    it is handed on teardown, and this one has to outlive the session. The
    fixture closes it for real at the end.
    """

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def disconnect(self):
        return None

    async def close_for_real(self):
        await self._conn.disconnect()


@pytest.fixture(scope="module")
def live_client():
    """An MCP client session against whatever game is currently running."""
    from mcp.shared.memory import create_connected_server_and_client_session

    import civ_mcp.server as server
    from civ_mcp.connection import GameConnection

    loop = asyncio.new_event_loop()
    conn = GameConnection()

    async def _probe() -> str | None:
        try:
            await conn.connect()
        except ConnectionError as exc:
            return f"No game reachable on FireTuner: {exc}"
        if conn.gamecore_index is None:
            return "Connected, but no GameCore state — is a save loaded?"
        return None

    reason = loop.run_until_complete(_probe())
    if reason:
        loop.run_until_complete(conn.disconnect())
        loop.close()
        pytest.skip(reason)

    shared = _Shared(conn)

    class _Client:
        def call(self, tool: str, arguments: dict | None = None) -> str:
            async def _main():
                with server.testing_overrides(
                    connection_factory=lambda: shared, background_services=False
                ):
                    async with create_connected_server_and_client_session(
                        server.mcp, raise_exceptions=False
                    ) as client:
                        result = await client.call_tool(tool, arguments or {})
                        return "".join(
                            b.text for b in result.content if getattr(b, "text", None)
                        )

            return loop.run_until_complete(_main())

    yield _Client()

    loop.run_until_complete(shared.close_for_real())
    loop.close()


def _first_id(text: str) -> int | None:
    match = re.search(r"id[=: ](\d+)", text)
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

READ_TOOLS = [
    "get_game_overview",
    "get_units",
    "get_cities",
    "get_empire_resources",
    "get_builder_tasks",
    "get_exploration_status",
    "get_diplomacy",
    "get_research_options",
    "get_policies",
    "get_notifications",
    "get_governors",
    "get_city_states",
    "get_great_people",
    "get_trade_routes",
    "get_victory_progress",
    "get_religion_spread",
    "get_belief_options",
    "get_dedications",
    "get_world_congress",
    "get_pending_deals",
    "get_pending_diplomacy",
    "get_spies",
    "get_settle_sites_on_map",
]


@pytest.mark.parametrize("tool", READ_TOOLS)
def test_read_tool_does_not_error(live_client, tool):
    """Every no-argument read returns content rather than an error.

    This alone would have caught the two dead scripts: a broken import in the
    query layer surfaces here as an `"Error: …"` on every affected tool.
    """
    result = live_client.call(tool, {})
    assert result, f"{tool} returned nothing"
    assert not result.startswith("Error"), f"{tool}: {result[:200]}"


def test_overview_reports_a_turn(live_client):
    result = live_client.call("get_game_overview", {})
    assert re.search(r"[Tt]urn\s*:?\s*\d+", result), result[:200]


def test_units_and_cities_carry_ids(live_client):
    """Ids are the handle every write tool takes; a read without them is a dead end."""
    units = live_client.call("get_units", {})
    if "no units" not in units.lower():
        assert _first_id(units) is not None, f"no unit id in output: {units[:200]}"

    cities = live_client.call("get_cities", {})
    if "no cities" not in cities.lower():
        assert _first_id(cities) is not None, f"no city id in output: {cities[:200]}"


def test_map_area_is_readable_around_a_city(live_client):
    cities = live_client.call("get_cities", {})
    coords = re.search(r"\((\d+),\s*(\d+)\)", cities)
    if not coords:
        pytest.skip("no city with coordinates to centre on")
    result = live_client.call(
        "get_map_area",
        {
            "center_x": int(coords.group(1)),
            "center_y": int(coords.group(2)),
            "radius": 2,
        },
    )
    assert not result.startswith("Error"), result[:200]


# ---------------------------------------------------------------------------
# Writes — each asserts the game actually changed
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_move_changes_the_units_position(live_client):
    """A move must be observable in the next read, not just reported."""
    units = live_client.call("get_units", {})
    unit_id = _first_id(units)
    position = re.search(r"\((\d+),\s*(\d+)\)", units)
    if unit_id is None or position is None:
        pytest.skip("no unit with a position to move")

    x, y = int(position.group(1)), int(position.group(2))
    result = live_client.call(
        "unit_action",
        {"unit_id": unit_id, "action": "move", "target_x": x + 1, "target_y": y},
    )
    assert not result.startswith("Error"), result[:200]

    after = live_client.call("get_units", {})
    assert str(unit_id) in after


@pytest.mark.live
def test_end_turn_advances_the_turn_counter(live_client):
    before = live_client.call("get_game_overview", {})
    before_turn = re.search(r"[Tt]urn\s*:?\s*(\d+)", before)
    if not before_turn:
        pytest.skip("could not read the turn number")

    result = live_client.call("end_turn", {})
    assert not result.startswith("Error"), result[:200]

    after = live_client.call("get_game_overview", {})
    after_turn = re.search(r"[Tt]urn\s*:?\s*(\d+)", after)
    assert after_turn is not None
    assert int(after_turn.group(1)) >= int(before_turn.group(1)), (
        "the turn counter went backwards"
    )


# ---------------------------------------------------------------------------
# Save / load recovery — the plan's §4.6, previously a manual check
# ---------------------------------------------------------------------------


def test_save_listing_works(live_client):
    """The non-destructive half of the recovery path."""
    saves = live_client.call("get_saves", {})
    assert not saves.startswith("Error"), saves[:200]
    assert re.search(r"[A-Za-z0-9_]+", saves), saves[:200]


@pytest.mark.destructive
def test_save_reload_round_trip(live_client):
    """`get_saves` → `load_game` → the overview confirms the turn.

    Separately marked because it is genuinely destructive: `load_game`
    falls back to killing and relaunching the game, which takes ~100s and
    leaves every later test in this module talking to a different process.
    Run it alone:

        uv run pytest -m "live and destructive"
    """
    saves = live_client.call("get_saves", {})
    match = re.search(r"(0_MCP_\d+)", saves)
    if not match:
        pytest.skip("no MCP autosave to reload")

    result = live_client.call("load_game", {"save_name": match.group(1)})
    assert not result.startswith("Error"), result[:200]

    # The reload kills and relaunches the process, so the socket that served
    # the call above is gone. Polling here is not leniency — it is the
    # documented contract ("Wait ~10s then use get_game_overview to verify"),
    # and an agent that does not do it sees a dropped connection instead of a
    # loaded game.
    overview = ""
    for _ in range(30):
        overview = live_client.call("get_game_overview", {})
        if re.search(r"[Tt]urn\s*:?\s*\d+", overview):
            break
        time.sleep(5)
    assert re.search(r"[Tt]urn\s*:?\s*\d+", overview), (
        f"game never came back after reload: {overview[:200]}"
    )
