"""Shared fixtures for the test suite.

Two seams, at different layers:

- ``make_game_state`` drives a ``GameState`` method directly with canned Lua
  responses. Cheap, and the right tool for logic that lives in one method.
- ``civ_server`` drives the real ``FastMCP`` server over an in-memory client
  session, so a call goes through argument validation, the tool body,
  ``GameState``, the ``lua/`` builders, the parsers and ``narrate.py`` before
  a string comes back. This is the seam that observes ``server.py``.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import civ_mcp.server as server
from civ_mcp.game_state import GameState

from utils import snapshots


def pytest_addoption(parser):
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        default=False,
        help="Rewrite snapshots from current output instead of asserting.",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: requires a running Civ 6 with FireTuner; excluded from CI",
    )
    config.addinivalue_line(
        "markers",
        "destructive: kills and relaunches the game; run on its own",
    )
    if config.getoption("--update-snapshots"):
        os.environ[snapshots.UPDATE_ENV] = "1"


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


class ToolClient:
    """Synchronous wrapper over an in-memory MCP client session.

    The suite's established style is a flat test function calling
    ``asyncio.run``; this keeps that shape rather than pulling in an async
    plugin for the handful of tests that need a session.
    """

    def __init__(self, connection, log_dir: Path):
        self._connection = connection
        self._log_dir = log_dir

    def _session(self):
        return create_connected_server_and_client_session(
            server.mcp, raise_exceptions=False
        )

    def _run(self, coro_factory):
        async def _main():
            with server.testing_overrides(
                connection_factory=lambda: self._connection,
                background_services=False,
                log_dir=self._log_dir,
            ):
                async with self._session() as client:
                    return await coro_factory(client)

        return asyncio.run(_main())

    def call(self, name: str, arguments: dict | None = None) -> str:
        """Call a tool and return its text, as the agent would receive it.

        Note that a *tool* error is not a *protocol* error: ``_logged``
        converts LuaError / ValueError / ConnectionError into an ``"Error: …"``
        string and returns it successfully. Assert on the text. Use
        ``call_raw`` when the distinction matters.
        """
        result = self.call_raw(name, arguments)
        return "".join(
            block.text for block in result.content if getattr(block, "text", None)
        )

    def call_raw(self, name: str, arguments: dict | None = None):
        """Call a tool and return the full MCP result, including ``isError``."""
        return self._run(lambda client: client.call_tool(name, arguments or {}))

    def script(self, calls: list[tuple[str, dict]]) -> list[str]:
        """Run several calls inside one session, returning their texts.

        Each ``call`` opens and closes its own session, which resets the
        ``GameState`` a tool holds. Anything that depends on state carried
        between calls — the seeded revealed-tile set, the per-turn advisor
        counter — has to run here instead.
        """

        async def _run_all(client):
            texts = []
            for name, arguments in calls:
                result = await client.call_tool(name, arguments or {})
                texts.append(
                    "".join(
                        block.text
                        for block in result.content
                        if getattr(block, "text", None)
                    )
                )
            return texts

        return self._run(_run_all)

    def list_tools(self):
        return self._run(lambda client: client.list_tools()).tools


@pytest.fixture
def civ_server(tmp_path):
    """Drive the real MCP server against a supplied fake connection.

    Returns a factory so a test chooses its own connection — a recording, a
    hand-built stub, or something that raises.
    """

    def _factory(connection):
        return ToolClient(connection, log_dir=tmp_path / "logs")

    return _factory
