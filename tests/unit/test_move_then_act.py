"""Tests for the move-then-act helper (stage 3.2).

Dispatching a builder used to take a move this turn and an improve the next.
A tile-acting verb given a target now walks there and acts in one call.

Three outcomes are distinct on purpose, because the agent's next call differs:
it acted; it did not arrive (MOVED_PARTIAL); it arrived with no movement left
and the verb refused (ARRIVED_WAITING).

The third case was measured on a live game at turn 73, not assumed: a builder
that arrives on its target with 0 movement is refused with "Builder has no
moves remaining this turn". The review had guessed this case would not exist.
"""

import asyncio

from civ_mcp.game_state import GameState


class ScriptedConnection:
    """Replays queued write/read responses in order.

    Position reads are queued explicitly; every other read the move path makes
    on its way (the revealed-tile seed, the post-move visibility diff) returns
    nothing. Those feed narration the helper does not branch on, so pinning
    their number here would couple these tests to move_unit's internals.
    """

    def __init__(self, write_lines=None, position_lines=None):
        self._write = list(write_lines or [])
        self._positions = list(position_lines or [])
        self.writes: list[str] = []

    async def execute_write(self, lua):
        self.writes.append(lua)
        return self._write.pop(0)

    async def execute_read(self, lua):
        if "POS|" not in lua or not self._positions:
            return []
        # The last entry stands for "where the unit ended up", and is returned
        # for every later read: move_unit does its own position readback
        # between the helper's two, and the two agree by construction.
        if len(self._positions) > 1:
            return self._positions.pop(0)
        return self._positions[0]


def make_gs(conn):
    gs = GameState.__new__(GameState)
    gs.conn = conn
    gs._revealed = set()  # already seeded, so no seed round trip
    return gs


def acted(result="IMPROVING|IMPROVEMENT_FARM|12,30"):
    async def _act():
        return result

    return _act


def test_unit_already_on_the_tile_acts_without_moving():
    conn = ScriptedConnection([], [["POS|12|30|2"]])
    gs = make_gs(conn)

    result = asyncio.run(gs.move_then_act(5, "improve", 12, 30, acted()))

    assert result == "IMPROVING|IMPROVEMENT_FARM|12,30"
    # No move was issued, so the result carries no "moved to" note.
    assert conn.writes == []


def test_arriving_runs_the_action_and_notes_the_move():
    conn = ScriptedConnection(
        [["OK:MOVING_TO|12,30|from:11,30"]],
        [["POS|11|30|2"], ["POS|12|30|1"]],
    )
    gs = make_gs(conn)

    result = asyncio.run(gs.move_then_act(5, "improve", 12, 30, acted()))

    assert result == "IMPROVING|IMPROVEMENT_FARM|12,30 (moved to 12,30)"


def test_not_arriving_reports_moved_partial_and_does_not_act():
    action_ran = False

    async def _act():
        nonlocal action_ran
        action_ran = True
        return "IMPROVING|IMPROVEMENT_FARM|14,31"

    conn = ScriptedConnection(
        [["OK:MOVING_TO|14,31|from:12,30"]],
        [["POS|12|30|2"], ["POS|13|30|0"]],
    )
    gs = make_gs(conn)

    result = asyncio.run(gs.move_then_act(5, "improve", 14, 31, _act))

    assert result.startswith("MOVED_PARTIAL|at=(13,30)|target=(14,31)|remaining=0")
    assert "Re-issue improve" in result
    assert not action_ran, "the action must not run away from its target tile"


def test_arriving_with_no_movement_left_reports_arrived_waiting():
    """The verb refused and the unit has no movement: do not re-issue a move."""
    conn = ScriptedConnection(
        [["OK:MOVING_TO|12,30|from:10,30"]],
        [["POS|10|30|2"], ["POS|12|30|0"]],
    )
    gs = make_gs(conn)

    result = asyncio.run(
        gs.move_then_act(
            5,
            "improve",
            12,
            30,
            acted("Error: CANNOT_IMPROVE|Builder has no moves remaining this turn"),
        )
    )

    assert result.startswith("ARRIVED_WAITING|at=(12,30)|remaining=0")
    assert "without moving" in result
    # The game's own diagnosis survives the wrapping.
    assert "no moves remaining" in result


def test_a_failure_with_movement_left_is_not_arrived_waiting():
    """A real refusal must not be disguised as a movement problem."""
    conn = ScriptedConnection(
        [["OK:MOVING_TO|12,30|from:11,30"]],
        [["POS|11|30|2"], ["POS|12|30|1"]],
    )
    gs = make_gs(conn)

    result = asyncio.run(
        gs.move_then_act(
            5,
            "improve",
            12,
            30,
            acted("Error: CANNOT_IMPROVE|tile has FEATURE_FLOODPLAINS_PLAINS"),
        )
    )

    assert "ARRIVED_WAITING" not in result
    assert "FEATURE_FLOODPLAINS_PLAINS" in result
    assert "(moved to 12,30)" in result


def test_a_missing_unit_is_reported_rather_than_moved():
    conn = ScriptedConnection([], [["POS|GONE"]])
    gs = make_gs(conn)

    result = asyncio.run(gs.move_then_act(5, "improve", 12, 30, acted()))

    assert "UNIT_GONE" in result


def test_position_read_tolerates_a_line_without_moves():
    """Recordings made before the moves field existed must still parse."""
    conn = ScriptedConnection([], [["POS|12|30"]])
    gs = make_gs(conn)

    assert asyncio.run(gs.read_unit_position(5)) == (12, 30, 0)
