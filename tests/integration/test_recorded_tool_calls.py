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
# A recording stores the parameters `_logged` was given, which is a log record
# rather than the call's arguments. For almost every tool the two are the same
# dict. Where they are not, the recording still proves the call succeeded, but
# it cannot be replayed, because replaying it means calling the tool with the
# log record instead of the arguments.
NOT_REPLAYABLE = {
    "end_turn": "polls the AI turn on a wall clock; query count is not "
    "determined by game state",
    "run_lua": "logs its context but not its code, so a replay would call it "
    "with no Lua to run",
    "test_deal": "propose_deal(mode='test') logs under this label with the "
    "deal expanded into items, not the arguments it was called with",
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
            "  uv run python scripts/record_game_traffic.py --scenario ground_control"
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
    # Germany (Frederick Barbarossa) at turn 355, Atomic era. The first
    # scenario that is at war, and the first with a pillaged tile, a damaged
    # unit, an Encampment, an Aerodrome and a Military Engineer — the
    # preconditions for `attack`, `repair`, `heal` and `build_route`.
    # Loaded by hand: the OCR navigation
    # cannot reach it, which is a launcher problem and not a save problem.
    "barb355": "0T_BARB_T355.Civ6Save",
    # Turn 361, and the save that holds the most: the Royal Society building,
    # idle spies, a spy already inside an enemy city, five Great People and an
    # idle trader. It is the only scenario that can record `spy_mission`,
    # which acts where the spy stands and so needs one that has arrived.
    "royalsociety": "0T_BARB_ROYALSOCIETY.Civ6Save",
    # Turn 352, at war, and the only save in the project that has ever held a
    # Spy — the tool that has been in the recording plan since the suite was
    # built and had never once been recorded.
    "barbwarspies": "0T_BARB_WARSPIES.Civ6Save",
    # Germany again, each save made to hold one state the Inca saves never
    # reach. Named for the condition rather than the turn, because that is
    # what decides which tools they can record.
    "barbwar": "0T_BARB_WAR.Civ6Save",
    "barbwar2": "0T_BARB_WAR2.Civ6Save",
    # Turn 126, played forward from barbwar2 until Methone had walls, a
    # *completed* Encampment and enemies inside both districts' range. It is
    # the only save that can record a city ranged attack at all, and the only
    # one that shows the Encampment firing as a second, independent strike.
    "barbencampment": "0T_BARB_ENCAMPMENT.Civ6Save",
    "barbunits": "0T_BARB_UNITS.Civ6Save",
    "barbcongress": "0T_BARB_CONGRESS.Civ6Save",
    "barbenvoy": "0T_BARB_ENVOY.Civ6Save",
    "barbdedication": "0T_BARB_DEDICATION.Civ6Save",
    "barbfreecity": "0T_BARB_FREECITY.Civ6Save",
    "barbpantheon": "0T_BARB_PANTHEON.Civ6Save",
    "turn37": "0T_TURN37_INCA.Civ6Save",
    # Turn 57 carries what turn 37 could not: a settler, a trader, a builder
    # with charges, walls, a met civ and a Holy Site. Those are the
    # preconditions for the builder and settler verbs that turn 37 leaves
    # unrecorded.
    "turn57": "0T_TURN57_INCA.Civ6Save",
    # Turn 63 is the first scenario with a religion founded, so it is the only
    # one where `religion_type` is non-empty and `get_religion_spread` prints
    # RELIGION_* rather than falling back to a display name. It also sees
    # Georgia's cities, which Stage 3.4 needs for foreign-city ids.
    "turn63": "0T_TURN63_INCA.Civ6Save",
    # Turn 73 has a builder standing where it can build, an idle trader and a
    # missionary — the preconditions for `builder_work(improve)` and
    # `send_unit_to_city`, which no earlier save could satisfy.
    "turn73": "0T_TURN73_INCA.Civ6Save",
    # Turn 73 again, with the Missionary walked into Antawaylla instead of
    # left standing two tiles outside it. `spread_religion` acts in place, so
    # every other save refuses it with CANNOT_SPREAD and no recording was ever
    # possible. This is the only scenario that can capture it.
    "incamissionary": "0T_INCA_MISSIONARY.Civ6Save",
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
