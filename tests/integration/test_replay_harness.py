"""The replay machinery itself, and the record-then-replay round trip.

Kept apart from the corpus tests so a failure is never ambiguous between "the
tool changed" and "the harness is broken". Nothing here touches
`tests/data/recordings/` — every recording is built in-test.

The round trip matters because the two halves live in different packages:
`src/civ_mcp/recording.py` writes, `tests/utils/recordings.py` reads, and
nothing else couples them. Without a test they can drift apart, and the drift
only shows up during a recording session with a live game — the most expensive
place to discover it.
"""

from __future__ import annotations

import pytest

from utils.recordings import (
    READ,
    WRITE,
    Exchange,
    Recording,
    RecordingExhausted,
    ReplayConnection,
)


def make_recording(tool="get_units", arguments=None, exchanges=(), result=None):
    """A Recording built in-test, with no file behind it."""
    return Recording(
        tool=tool,
        arguments=arguments or {},
        exchanges=[
            Exchange(context=c, lua=lua, lines=lines) for c, lua, lines in exchanges
        ],
        result=result,
        scenario="synthetic",
    )


# ---------------------------------------------------------------------------
# The machinery
# ---------------------------------------------------------------------------


def test_reads_and_writes_have_independent_queues():
    """A write must not consume a read's recorded response.

    Tools interleave the two in ways that depend on control flow, so a single
    queue would desynchronise whenever an optional branch changed.
    """
    recording = make_recording(
        exchanges=[
            (READ, "read-1", ["R1"]),
            (WRITE, "write-1", ["W1"]),
            (READ, "read-2", ["R2"]),
        ]
    )
    conn = ReplayConnection(recording)

    import asyncio

    assert asyncio.run(conn.execute_write("anything")) == ["W1"]
    assert asyncio.run(conn.execute_read("anything")) == ["R1"]
    assert asyncio.run(conn.execute_read("anything")) == ["R2"]


def test_lua_text_is_not_the_matching_key():
    """Sending different Lua than was recorded still replays.

    Stages 1b and 3.4 deliberately change the Lua. Keyed on text, every such
    edit would break every recording and train whoever runs the suite to
    regenerate without reading the diff.
    """
    import asyncio

    conn = ReplayConnection(make_recording(exchanges=[(READ, "original lua", ["X"])]))
    assert asyncio.run(conn.execute_read("completely different lua")) == ["X"]


def test_running_off_the_end_names_the_unanswered_query():
    """`IndexError: pop from empty list` is not a diagnosis."""
    import asyncio

    conn = ReplayConnection(make_recording(exchanges=[(READ, "only one", ["X"])]))
    asyncio.run(conn.execute_read("first"))

    with pytest.raises(RecordingExhausted) as excinfo:
        asyncio.run(conn.execute_read("the query with no recorded answer"))

    message = str(excinfo.value)
    assert "the query with no recorded answer" in message
    assert "get_units" in message, "the failure should name the tool"


def test_unused_exchanges_are_reported():
    """A tool that stopped issuing a query should be visible, not silent."""
    import asyncio

    conn = ReplayConnection(
        make_recording(exchanges=[(READ, "a", ["A"]), (READ, "b", ["B"])])
    )
    asyncio.run(conn.execute_read("a"))
    assert [e.lua for e in conn.unused()] == ["b"]


def test_recording_survives_a_json_round_trip(tmp_path):
    recording = make_recording(
        exchanges=[(READ, "lua", ["L1", "L2"])], result="narrated text"
    )
    path = tmp_path / "c.json"
    recording.write(path)
    loaded = Recording.load(path)
    assert loaded.tool == recording.tool
    assert loaded.result == "narrated text"
    assert loaded.exchanges[0].lines == ["L1", "L2"]


# ---------------------------------------------------------------------------
# Record → replay round trip
# ---------------------------------------------------------------------------


class ScriptedGame:
    """A stand-in game that answers reads and writes from fixed tables."""

    gamecore_index = 0
    ingame_index = 1

    def __init__(self, read_lines, write_lines=None):
        self._read_lines = read_lines
        self._write_lines = write_lines or []

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def execute_read(self, lua, timeout=5.0):
        from civ_mcp import recording

        lines = list(self._read_lines)
        recording.record(recording.READ, lua, lines)
        return lines

    async def execute_write(self, lua, timeout=5.0):
        from civ_mcp import recording

        lines = list(self._write_lines)
        recording.record(recording.WRITE, lua, lines)
        return lines


def test_recorder_output_is_replayable(civ_server, tmp_path, monkeypatch):
    """What `recording.py` writes, `replay.py` must be able to read back.

    Without this the two halves can drift apart and the failure only shows up
    during a recording session with a live game — the most expensive place to
    discover it.
    """
    from civ_mcp import recording

    recording.enable(tmp_path, scenario="round_trip")
    try:
        civ_server(ScriptedGame(read_lines=["ANYTHING|1"])).call("get_units", {})
    finally:
        recording.disable()

    written = list((tmp_path / "round_trip").glob("*.json"))
    assert written, "the recorder produced no recording"

    recording = Recording.load(written[0])
    assert recording.tool == "get_units"
    assert recording.scenario == "round_trip"
    assert recording.exchanges, "no Lua round trips were captured"
    assert recording.result is not None, "the narrated result was not recorded"

    # And the recording drives the same tool a second time.
    replayed = civ_server(ReplayConnection(recording)).call("get_units", {})
    assert replayed == recording.result


def test_recording_is_off_by_default(civ_server, tmp_path):
    """The recorder must be inert unless explicitly switched on."""
    from civ_mcp import recording

    assert not recording.is_recording()
    civ_server(ScriptedGame(read_lines=["X|1"])).call("get_units", {})
    assert not list(tmp_path.glob("**/*.json"))


def test_traffic_outside_a_tool_call_is_not_attributed(tmp_path):
    """Background pollers must not land in whichever recording is open."""
    from civ_mcp import recording

    recording.enable(tmp_path, scenario="s")
    try:
        recording.record(recording.READ, "poller query", ["noise"])
        recording.begin("get_units", {})
        recording.record(recording.READ, "tool query", ["signal"])
        recording.finish("result")
    finally:
        recording.disable()

    recording = Recording.load(tmp_path / "s" / "get_units.json")
    assert [e.lua for e in recording.exchanges] == ["tool query"]


def test_recording_filenames_distinguish_dispatcher_verbs(tmp_path):
    """One recording per verb, or each recording overwrites the last."""
    from civ_mcp import recording

    recording.enable(tmp_path, scenario="s")
    try:
        for action in ("fortify", "skip"):
            recording.begin("unit_action", {"unit_id": 1, "action": action})
            recording.record(recording.WRITE, "lua", ["OK"])
            recording.finish("done")
    finally:
        recording.disable()

    written = sorted(p.name for p in (tmp_path / "s").glob("*.json"))
    assert written == ["unit_action__fortify.json", "unit_action__skip.json"]


