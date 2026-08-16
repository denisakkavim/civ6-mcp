"""Tests for the implicit placement path in set_city_production (stage 3.1).

Placing a district used to take two calls: ask the advisor, then copy its
coordinates into set_city_production. Omitting the coordinates now makes the
server ask the advisor itself and build on the top-ranked tile.

Districts resolve before the write, because a district always needs a tile.
Wonders resolve after it: only the game database knows which buildings are
wonders, so the server lets the game say so and retries. An ordinary building
therefore never pays for the advisor.
"""

import asyncio

import pytest

from civ_mcp.game_state import GameState


class RecordingConnection:
    """Replays queued responses and keeps the Lua it was asked to run."""

    def __init__(self, write_lines=None, read_lines=None):
        self._write = list(write_lines or [])
        self._read = list(read_lines or [])
        self.writes: list[str] = []
        self.reads: list[str] = []

    async def execute_write(self, lua):
        self.writes.append(lua)
        return self._write.pop(0)

    async def execute_read(self, lua):
        self.reads.append(lua)
        return self._read.pop(0)


def make_gs(conn):
    """A GameState carrying only the fields the placement path reads."""
    gs = GameState.__new__(GameState)
    gs.conn = conn
    gs._advisor_calls_this_turn = 0
    gs._advisor_budget_warning = None
    return gs


DISTRICT_SITES = [
    "DPLOT|12,30|3|0|0|0|0|3|Plains Hills",
    "DPLOT|13,31|1|0|0|0|0|1|Grassland",
]

WONDER_SITES = [
    "WPLOT|45,13|TERRAIN_PLAINS|FEATURE_FOREST|false|false|none|none|2",
    "WPLOT|44,12|TERRAIN_PLAINS|none|true|false|none|none|3",
]


def test_district_without_a_tile_uses_the_advisors_top_pick():
    conn = RecordingConnection(
        [DISTRICT_SITES, ["OK:PRODUCING|DISTRICT_CAMPUS|5 turns"]],
        [["CONFIRMED|5 turns"]],
    )
    gs = make_gs(conn)

    result = asyncio.run(gs.set_city_production(65536, "DISTRICT", "DISTRICT_CAMPUS"))

    assert result.startswith("PRODUCING|DISTRICT_CAMPUS|5 turns")
    assert "auto-placed at (12,30)" in result
    assert "Adj +3" in result
    # The top-ranked tile, not the second one, reached the produce call.
    assert "PARAM_X] = 12" in conn.writes[1]
    assert "PARAM_Y] = 30" in conn.writes[1]


def test_explicit_coordinates_skip_the_advisor():
    conn = RecordingConnection(
        [["OK:PRODUCING|DISTRICT_CAMPUS|5 turns"]],
        [["CONFIRMED|5 turns"]],
    )
    gs = make_gs(conn)

    result = asyncio.run(
        gs.set_city_production(65536, "DISTRICT", "DISTRICT_CAMPUS", 20, 40)
    )

    assert result == "PRODUCING|DISTRICT_CAMPUS|5 turns"
    assert "auto-placed" not in result
    assert len(conn.writes) == 1
    assert gs._advisor_calls_this_turn == 0


def test_district_with_no_valid_tile_reports_it_without_producing():
    conn = RecordingConnection([[]], [])
    gs = make_gs(conn)

    result = asyncio.run(gs.set_city_production(65536, "DISTRICT", "DISTRICT_CAMPUS"))

    assert "NO_PLACEMENT" in result
    assert "get_district_sites" in result
    # Only the advisor ran; nothing was sent to the build queue.
    assert len(conn.writes) == 1


def test_wonder_retries_with_a_tile_after_the_game_asks_for_one():
    conn = RecordingConnection(
        [
            ["ERR:MISSING_COORDS|BUILDING_APADANA is a wonder and requires target_x"],
            WONDER_SITES,
            ["OK:PRODUCING|BUILDING_APADANA|14 turns"],
        ],
        [["CONFIRMED|14 turns"]],
    )
    gs = make_gs(conn)

    result = asyncio.run(gs.set_city_production(65536, "BUILDING", "BUILDING_APADANA"))

    assert result.startswith("PRODUCING|BUILDING_APADANA|14 turns")
    assert "auto-placed at (45,13)" in result
    assert "displacement 2" in result
    assert "PARAM_X] = 45" in conn.writes[2]


def test_ordinary_building_never_reaches_the_advisor():
    conn = RecordingConnection(
        [["OK:PRODUCING|BUILDING_SHRINE|2 turns"]],
        [["CONFIRMED|2 turns"]],
    )
    gs = make_gs(conn)

    result = asyncio.run(gs.set_city_production(65536, "BUILDING", "BUILDING_SHRINE"))

    assert result == "PRODUCING|BUILDING_SHRINE|2 turns"
    assert len(conn.writes) == 1
    assert gs._advisor_calls_this_turn == 0


def test_a_failed_build_carries_no_placement_note():
    """The note names a tile the server chose; a refusal did not build on it."""
    conn = RecordingConnection(
        [DISTRICT_SITES, ["ERR:CANNOT_PRODUCE|DISTRICT_CAMPUS cannot be produced"]],
        [],
    )
    gs = make_gs(conn)

    result = asyncio.run(gs.set_city_production(65536, "DISTRICT", "DISTRICT_CAMPUS"))

    assert "CANNOT_PRODUCE" in result
    assert "auto-placed" not in result


@pytest.mark.parametrize("calls_already_made", [0, GameState.ADVISOR_BUDGET_HARD + 5])
def test_implicit_advisor_call_counts_but_never_gates(calls_already_made):
    """The cap belongs to the explicit advisor tools.

    Gating here would fail a production call because of a rate limit on a
    different tool the agent had already spent elsewhere.
    """
    conn = RecordingConnection(
        [DISTRICT_SITES, ["OK:PRODUCING|DISTRICT_CAMPUS|5 turns"]],
        [["CONFIRMED|5 turns"]],
    )
    gs = make_gs(conn)
    gs._advisor_calls_this_turn = calls_already_made

    result = asyncio.run(gs.set_city_production(65536, "DISTRICT", "DISTRICT_CAMPUS"))

    assert result.startswith("PRODUCING|DISTRICT_CAMPUS")
    assert "BUDGET" not in result
    assert gs._advisor_calls_this_turn == calls_already_made + 1
