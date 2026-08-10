"""Tests for set_city_production silent-failure detection.

The game's CityManager.RequestOperation is fire-and-forget; even when
CanStartOperation returned true, it can silently no-op if the queue is
in a degenerate state. The OK-path verification catches this.
"""

import asyncio


def test_ok_path_is_verified(make_game_state):
    """CanStartOperation=true and verify confirms → return the original OK."""
    gs = make_game_state(
        [["OK:PRODUCING|BUILDING_MONUMENT|6 turns"]],
        [["CONFIRMED|6 turns"]],
    )
    result = asyncio.run(gs.set_city_production(65536, "BUILDING", "BUILDING_MONUMENT"))
    assert result == "PRODUCING|BUILDING_MONUMENT|6 turns"


def test_ok_path_catches_silent_failure(make_game_state):
    """Lua returns OK but verify reads NOT_SET → SILENT_FAILURE error."""
    gs = make_game_state(
        [["OK:PRODUCING|UNIT_TRADER|1 turns"]],
        [["NOT_SET|current=nil|expected=UNIT_TRADER"]],
    )
    result = asyncio.run(gs.set_city_production(262145, "UNIT", "UNIT_TRADER"))
    assert "SILENT_FAILURE" in result
    assert "UNIT_TRADER" in result
    assert "purchase_item" in result


def test_hard_error_bypasses_verification(make_game_state):
    """A CanProduce failure never reaches the verify path."""
    gs = make_game_state(
        [
            [
                "ERR:CANNOT_PRODUCE|BUILDING_UNIVERSITY cannot be produced "
                "(requires DISTRICT_CAMPUS district)"
            ]
        ],
        [],  # verify is never called
    )
    result = asyncio.run(
        gs.set_city_production(65536, "BUILDING", "BUILDING_UNIVERSITY")
    )
    assert "CANNOT_PRODUCE" in result


def test_verification_failure_falls_through_optimistically(make_game_state):
    """If verify itself throws, return the original OK rather than an error."""

    class ThrowingConnection:
        async def execute_write(self, lua):
            return ["OK:PRODUCING|UNIT_WARRIOR|2 turns"]

        async def execute_read(self, lua):
            raise RuntimeError("connection dropped")

    gs = make_game_state(conn=ThrowingConnection())
    result = asyncio.run(gs.set_city_production(65536, "UNIT", "UNIT_WARRIOR"))
    assert result == "PRODUCING|UNIT_WARRIOR|2 turns"
