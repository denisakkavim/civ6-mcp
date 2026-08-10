"""Tests for post-move discovery feedback.

This feature shipped in 782de1a and never once executed: its only seeding
call site referenced an `lq` name that server.py never imported, so the
NameError was swallowed by a bare `except Exception` at debug level and the
revealed-set flag stayed false forever. These tests exist so a silent
regression of that shape fails loudly instead.
"""

import asyncio

import pytest

from civ_mcp.game_state import GameState

# A move from (12,30) to (13,30) that the engine accepts.
MOVE_OK = ["OK:MOVING_TO|13,30|from:12,30"]
POSITION = ["POS|13|30|"]


def tile(x, y, terrain="TERRAIN_GRASS", resource="none"):
    """One TILE| line as the post-move visibility query emits it."""
    return f"TILE|{x},{y}|{terrain}|none|{resource}|0|0|none|none"


@pytest.fixture
def make_gs(make_game_state, monkeypatch):
    """A GameState ready to move a unit, with no revealed tiles seeded yet."""

    async def _no_popup(self):
        return None

    monkeypatch.setattr(GameState, "dismiss_popup", _no_popup)

    def _factory(write_lines=None, read_lines=None, conn=None):
        gs = make_game_state(write_lines, read_lines, conn)
        gs._revealed = None
        return gs

    return _factory


def test_seeds_revealed_set_before_first_move(make_gs):
    """The revealed set is populated from the game, not left empty."""
    gs = make_gs(
        [MOVE_OK],
        [["REVEALED|12,30;13,30"], POSITION, [tile(12, 30), tile(13, 30)]],
    )
    asyncio.run(gs.move_unit(1, 13, 30))
    assert gs._revealed == {(12, 30), (13, 30)}


def test_seeds_only_once_per_game(make_gs):
    """A second move reuses the set instead of re-scanning the whole map."""
    gs = make_gs(
        [MOVE_OK, MOVE_OK],
        [["REVEALED|12,30"], POSITION, [tile(13, 30)], POSITION, [tile(13, 30)]],
    )
    asyncio.run(gs.move_unit(1, 13, 30))
    reads_after_first = gs.conn.reads_issued
    asyncio.run(gs.move_unit(1, 13, 30))
    # Position readback + visibility query only — no re-seed.
    assert gs.conn.reads_issued - reads_after_first == 2


def test_seed_failure_is_logged_not_swallowed(make_gs, caplog):
    """A failing seed warns rather than disabling the feature quietly."""

    class ThrowingConnection:
        async def execute_write(self, lua):
            return MOVE_OK

        async def execute_read(self, lua):
            raise RuntimeError("gamecore unavailable")

    gs = make_gs(None, None, conn=ThrowingConnection())
    result = asyncio.run(gs.move_unit(1, 13, 30))
    assert "MOVING_TO" in result, "the move itself must still be reported"
    assert any("Failed to seed revealed tiles" in r.message for r in caplog.records)


def test_newly_revealed_tiles_are_narrated(make_gs):
    """Tiles absent from the revealed set produce discovery text."""
    gs = make_gs(
        [MOVE_OK],
        [
            ["REVEALED|12,30"],
            POSITION,
            [
                tile(12, 30),
                tile(14, 30, resource="RESOURCE_IRON:RESOURCECLASS_STRATEGIC"),
            ],
        ],
    )
    result = asyncio.run(gs.move_unit(1, 13, 30))
    assert "MOVING_TO" in result
    assert "\n" in result, "discovery text should be appended on a new line"
    assert (14, 30) in gs._revealed


def test_already_revealed_tiles_stay_silent(make_gs):
    """Re-walking known ground appends nothing."""
    gs = make_gs(
        [MOVE_OK],
        [["REVEALED|12,30;13,30;14,30"], POSITION, [tile(13, 30), tile(14, 30)]],
    )
    result = asyncio.run(gs.move_unit(1, 13, 30))
    assert "\n" not in result


def test_blocked_move_skips_the_visibility_diff(make_gs):
    """A blocked move reveals nothing, so no visibility query is issued."""
    gs = make_gs(
        [["OK:MOVING_TO|13,30|from:12,30"]],
        [["REVEALED|12,30"], ["POS|12|30|"]],  # same tile as `from` → BLOCKED
    )
    result = asyncio.run(gs.move_unit(1, 13, 30))
    assert "BLOCKED" in result
    assert gs.conn.reads_issued == 2


def test_hill_terrain_is_not_labelled_twice(make_gs):
    """TERRAIN_PLAINS_HILLS already says Hills; the flag must not repeat it.

    The tile carries a resource because only "notable" tiles are described
    individually — the rest are just counted.
    """
    gs = make_gs(
        [MOVE_OK],
        [
            ["REVEALED|12,30"],
            POSITION,
            [
                "TILE|14,30|TERRAIN_PLAINS_HILLS|none|"
                "RESOURCE_IRON:RESOURCECLASS_STRATEGIC|1|0|none|none"
            ],
        ],
    )
    result = asyncio.run(gs.move_unit(1, 13, 30))
    assert "Hills Hills" not in result
    assert "Plains Hills" in result


def test_save_load_clears_the_revealed_set():
    """An older save has revealed less, so the set must be re-seeded."""
    gs = GameState.__new__(GameState)
    gs._revealed = {(1, 1), (2, 2)}
    gs._high_water_turn = 40
    gs._save_load_history = []
    gs._record_save_load("0_MCP_0040")
    assert gs._revealed is None
