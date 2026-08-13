"""Replay the recorded corpus through the whole stack.

Each recording drives the real tool — argument validation, tool body,
`GameState`, the `lua/` builders, the parsers, `narrate.py` — with the game
replaced by a tape. The narration is then compared against its snapshot, so a
change in what the agent reads shows up as a readable diff.

The corpus lives in `tests/data/recordings/<scenario>/` and is produced by
`scripts/record_game_traffic.py` against a live game. If it is empty these
tests report that loudly rather than passing quietly — a recording is only
worth having if it holds what the game actually said.
"""

from __future__ import annotations

import pytest

from utils import paths, recordings, snapshots
from utils.recordings import ReplayConnection

# ---------------------------------------------------------------------------
# The recorded corpus
# ---------------------------------------------------------------------------

CORPUS = recordings.available()

# Tools whose query count is not a function of game state, so a positional
# recording cannot reproduce their control flow.
#
# `end_turn` polls for the AI turn to finish on a wall clock, with escalating
# sleeps out to ~9 minutes, breaking as soon as the turn number advances. How
# many times it polls depends on how long the AI took on the recording
# machine. Replayed, it desynchronises after the first poll and then blocks in
# the sleep ladder. Its recording is still kept and still checked for a
# clean result below — only the narration replay is skipped.
NOT_REPLAYABLE = {
    "end_turn": "polls the AI turn on a wall clock; query count is not "
    "determined by game state",
}


def test_corpus_exists():
    """Fails until someone records against a live game.

    Marked xfail so the suite is green while the corpus is pending, and goes
    red the moment recordings land — which is the prompt to check them in and
    delete this marker.
    """
    if not CORPUS:
        pytest.xfail(
            "No recordings yet. Run:\n"
            "  uv run python scripts/install_saves.py\n"
            "  # launch Civ 6 (EnableTuner=1), load 0A_GROUND_CONTROL\n"
            "  uv run python scripts/record_game_traffic.py --scenario ground_control\n"
            "Record the dispatcher recordings (--dispatchers-only) BEFORE Stage 4 "
            "deletes unit_action / spy_action."
        )
    assert CORPUS


@pytest.mark.parametrize(
    "scenario,name", CORPUS, ids=[f"{s}/{n}" for s, n in CORPUS] or None
)
def test_recording_replays_to_its_snapshot(civ_server, scenario, name):
    """The tool, driven by recorded traffic, still produces what it produced.

    A diff here is a change in what the agent reads — the thing Stage 1b
    changes on purpose and everything else changes by accident.
    """
    recording = recordings.load(scenario, name)
    if recording.tool in NOT_REPLAYABLE:
        pytest.skip(f"{recording.tool}: {NOT_REPLAYABLE[recording.tool]}")

    conn = ReplayConnection(recording)
    actual = civ_server(conn).call(recording.tool, recording.arguments)

    snapshots.check_text(f"narration/{scenario}/{name}.txt", actual)

    leftover = conn.unused()
    assert not leftover, (
        f"{recording.tool} stopped issuing {len(leftover)} recorded "
        f"{'query' if len(leftover) == 1 else 'queries'}. If that is "
        f"intended, re-record; if not, a query was dropped."
    )


# Which save each scenario was recorded from. A recording is only
# re-recordable against the state it came from, so losing the save turns the
# corpus into something nobody can regenerate or extend.
SCENARIO_SAVES = {
    "turn37": "0T_TURN37_INCA.Civ6Save",
    # Turn 57 carries what turn 37 could not: a settler, a trader, a builder
    # with charges, walls, a met civ and a Holy Site. Those are the
    # preconditions for the `unit_action` verbs turn 37 leaves unrecorded, and
    # Stage 4 replaces every one of them.
    "turn57": "0T_TURN57_INCA.Civ6Save",
    # Turn 63 is the first scenario with a religion founded, so it is the only
    # one where `religion_type` is non-empty and `get_religion_spread` prints
    # RELIGION_* rather than falling back to a display name. It also sees
    # Georgia's cities, which Stage 3.4 needs for foreign-city ids.
    "turn63": "0T_TURN63_INCA.Civ6Save",
    # Turn 73 has a builder standing where it can build, an idle trader and a
    # missionary — the preconditions for `improve` and `teleport`, which no
    # earlier save could satisfy.
    "turn73": "0T_TURN73_INCA.Civ6Save",
    "round_trip": None,  # written by a test, not recorded from a game
}


@pytest.mark.parametrize("scenario", sorted({s for s, _ in CORPUS}) or ["turn37"])
def test_scenario_has_a_tracked_source_save(scenario):
    """Every recorded scenario names a save that is checked in."""
    assert scenario in SCENARIO_SAVES, (
        f"Scenario '{scenario}' has recordings but no entry in SCENARIO_SAVES. "
        f"Record which save it came from, or the corpus cannot be regenerated."
    )
    save = SCENARIO_SAVES[scenario]
    if save is None:
        return
    path = paths.SAVES / save
    assert path.exists(), (
        f"Scenario '{scenario}' was recorded from {save}, which is missing from "
        f"tests/data/saves/. Without it the corpus cannot be re-recorded."
    )


@pytest.mark.parametrize(
    "scenario,name", CORPUS, ids=[f"{s}/{n}" for s, n in CORPUS] or None
)
def test_recording_captured_a_successful_call(scenario, name):
    """A recording recorded from a failed call is a trap, not a fixture."""
    recording = recordings.load(scenario, name)
    assert recording.result is not None
    assert not recording.result.startswith("Error"), (
        f"{scenario}/{name} recorded a failed call: {recording.result[:120]!r}. "
        f"Re-record it against a game state where the call succeeds, or delete it."
    )
