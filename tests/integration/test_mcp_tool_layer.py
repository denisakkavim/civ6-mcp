"""Behaviour of the MCP tool layer itself — `server.py`, which nothing covered.

These tests need no recording. They pin the contracts every tool inherits
from `_logged`, and they prove the in-process harness reaches the real tool
bodies (if it did not, every replay test would pass vacuously).
"""

from __future__ import annotations

import pytest

import civ_mcp.server as server
from civ_mcp.connection import LuaError


class RecordingConnection:
    """Answers every query with fixed lines and remembers what was asked."""

    gamecore_index = 0
    ingame_index = 1

    def __init__(self, read_lines=None, write_lines=None):
        self._read_lines = read_lines or []
        self._write_lines = write_lines or []
        self.reads: list[str] = []
        self.writes: list[str] = []

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def execute_read(self, lua, timeout=5.0):
        self.reads.append(lua)
        return list(self._read_lines)

    async def execute_write(self, lua, timeout=5.0):
        self.writes.append(lua)
        return list(self._write_lines)


class RaisingConnection(RecordingConnection):
    """Fails every query with a chosen exception."""

    def __init__(self, exc):
        super().__init__()
        self._exc = exc

    async def execute_read(self, lua, timeout=5.0):
        raise self._exc

    async def execute_write(self, lua, timeout=5.0):
        raise self._exc


def test_harness_reaches_the_real_tool_body(civ_server):
    """A tool call issues real Lua, not a stub's idea of it."""
    conn = RecordingConnection()
    civ_server(conn).call("get_units", {})
    assert conn.reads or conn.writes, "the tool never queried the game"
    assert any(
        "print(" in lua for lua in conn.reads + conn.writes
    ), "the query does not look like the real Lua builders' output"


def test_all_tools_are_registered(civ_server):
    """The client sees the same surface the server defines."""
    tools = civ_server(RecordingConnection()).list_tools()
    names = {t.name for t in tools}
    assert "get_game_overview" in names
    assert "end_turn" in names
    assert len(names) == len(tools), "duplicate tool names registered"


# ---------------------------------------------------------------------------
# `_logged`'s contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [LuaError("bad lua"), ValueError("bad value")],
    ids=["lua", "value"],
)
def test_game_errors_come_back_as_prefixed_text(civ_server, exc):
    """`_logged` converts game-level failures into a readable agent-facing string.

    This is the property that lets an agent recover mid-turn rather than
    seeing an MCP transport failure. It also means `isError` is the wrong
    thing for a test to assert on.
    """
    result = civ_server(RaisingConnection(exc)).call_raw("get_units", {})
    text = "".join(b.text for b in result.content if getattr(b, "text", None))
    assert result.isError is False, "a game-level failure must not be a protocol error"
    assert text.startswith("Error:"), text


def test_connection_errors_lose_the_error_prefix(civ_server):
    """Pins a real inconsistency in `_logged` rather than asserting the ideal.

    `LuaError` and `ValueError` return `f"Error: {e}"`; the `ConnectionError`
    branch returns `str(e)` bare. An agent (or a `logger.log_tool_call`
    success check, which tests `result.startswith(("Error", "ERR"))`) that
    pattern-matches on the prefix therefore counts a dropped connection as a
    *successful* call.

    Change this test when the inconsistency is fixed — do not delete it.
    """
    result = civ_server(RaisingConnection(ConnectionError("game gone"))).call_raw(
        "get_units", {}
    )
    text = "".join(b.text for b in result.content if getattr(b, "text", None))
    assert result.isError is False
    assert text == "game gone"
    assert not text.startswith("Error:"), "inconsistency fixed — update this test"


def test_unexpected_exceptions_are_not_swallowed(civ_server):
    """`_logged` catches three types by name; anything else must surface.

    A bare `except Exception` here would hide real bugs — the exact failure
    mode that let the post-move discovery feature ship broken for months.
    """
    result = civ_server(RaisingConnection(RuntimeError("boom"))).call_raw(
        "get_units", {}
    )
    text = "".join(b.text for b in result.content if getattr(b, "text", None))
    assert result.isError is True
    assert "boom" in text


# ---------------------------------------------------------------------------
# Argument validation happens at the schema, not in the tool body
# ---------------------------------------------------------------------------


def test_wrong_typed_argument_is_rejected_before_the_tool_runs(civ_server):
    """Validation is the schema's job — the game is never contacted."""
    conn = RecordingConnection()
    result = civ_server(conn).call_raw(
        "get_map_area", {"center_x": "not-an-int", "center_y": 3}
    )
    assert result.isError is True
    assert not conn.reads and not conn.writes, (
        "a malformed call reached the game; validation is not being enforced "
        "at the schema"
    )


def test_missing_required_argument_is_rejected(civ_server):
    conn = RecordingConnection()
    result = civ_server(conn).call_raw("get_map_area", {})
    assert result.isError is True
    assert not conn.reads and not conn.writes


def test_unknown_tool_is_an_error(civ_server):
    result = civ_server(RecordingConnection()).call_raw("no_such_tool", {})
    assert result.isError is True


# ---------------------------------------------------------------------------
# The harness must not touch the developer's real state
# ---------------------------------------------------------------------------


def test_tool_call_log_goes_to_the_test_directory(civ_server, tmp_path):
    """`GameLogger` writes JSONL per call; a test run must not pollute ~/.civ6-mcp."""
    civ_server(RecordingConnection()).call("get_units", {})
    written = list((tmp_path / "logs").glob("*.jsonl"))
    assert written, "the tool-call log was not written where the test pointed it"
    assert "get_units" in written[0].read_text()


def test_background_services_stay_off(civ_server, monkeypatch):
    """Camera and popup watcher must not start under test.

    Both poll the connection on a timer. Left running, they interleave
    unrequested traffic with the tool's own and a positional recording
    desynchronises. Asserting on the connection's traffic would pass
    vacuously (the pollers may simply not have ticked yet), so this asserts
    that `start()` was never called at all.
    """
    started: list[str] = []
    for cls in (server.CameraController, server.PopupWatcher):
        monkeypatch.setattr(
            cls, "start", lambda self, _n=cls.__name__: started.append(_n)
        )

    civ_server(RecordingConnection()).call("get_units", {})
    assert started == [], f"background services started under test: {started}"


def test_the_services_guard_is_wired_to_something(civ_server, monkeypatch):
    """Counterpart to the test above: prove the guard is what suppresses them.

    Without this, deleting `_background_services_enabled` entirely would
    leave the previous test green.
    """
    started: list[str] = []
    for cls in (server.CameraController, server.PopupWatcher):
        monkeypatch.setattr(
            cls, "start", lambda self, _n=cls.__name__: started.append(_n)
        )
    monkeypatch.setattr(server, "_background_services_enabled", True)

    # Re-enter the override with services on, mimicking production.
    client = civ_server(RecordingConnection())
    original = server.testing_overrides

    def _services_on(**kwargs):
        kwargs["background_services"] = True
        return original(**kwargs)

    monkeypatch.setattr(server, "testing_overrides", _services_on)
    client.call("get_units", {})
    assert sorted(started) == ["CameraController", "PopupWatcher"]
