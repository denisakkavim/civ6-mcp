"""Shared fixtures for the test suite."""

import pytest

from civ_mcp.game_state import GameState


class StubConnection:
    """Replays queued write/read responses and counts the reads issued.

    Stands in for GameConnection so parser and action logic can be tested
    without a running game.
    """

    def __init__(self, write_lines=None, read_lines=None):
        self._write = list(write_lines or [])
        self._read = list(read_lines or [])
        self.reads_issued = 0

    async def execute_write(self, lua):
        return self._write.pop(0)

    async def execute_read(self, lua):
        self.reads_issued += 1
        return self._read.pop(0)


@pytest.fixture
def make_game_state():
    """Build a bare GameState whose connection replays canned responses.

    Bypasses __init__ deliberately: tests set only the attributes the code
    under test reads, so an unrelated field appearing in __init__ cannot
    silently change what a test exercises.
    """

    def _factory(write_lines=None, read_lines=None, conn=None):
        gs = GameState.__new__(GameState)
        gs.conn = conn or StubConnection(write_lines, read_lines)
        return gs

    return _factory
