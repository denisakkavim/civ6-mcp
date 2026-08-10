"""Tests for save-scumming detection in end_turn.py.

Validates that _check_save_scumming correctly distinguishes:
- Clean play (no saves)
- Legitimate deadlock debugging (many loads at a single turn cluster)
- Save scumming (loads spread across distinct turns)
"""

import types

import pytest

from civ_mcp.end_turn import _check_save_scumming


@pytest.fixture
def make_gs():
    """Build a stand-in carrying only the fields the check reads."""

    def _factory(history):
        return types.SimpleNamespace(_save_load_history=history, _run_aborted=False)

    return _factory


@pytest.fixture
def loads_at_turns():
    """Build a save-load history from a list of turn numbers."""

    def _factory(turns):
        return [
            (1000.0 + i, turn, f"AutoSave_{turn:04d}") for i, turn in enumerate(turns)
        ]

    return _factory


def test_no_history_is_quiet(make_gs):
    events, hard_stop = _check_save_scumming(make_gs([]))
    assert events == []
    assert hard_stop is False


def test_single_load_is_quiet(make_gs, loads_at_turns):
    events, hard_stop = _check_save_scumming(make_gs(loads_at_turns([100])))
    assert events == []
    assert hard_stop is False


def test_two_loads_are_quiet(make_gs, loads_at_turns):
    events, hard_stop = _check_save_scumming(make_gs(loads_at_turns([100, 101])))
    assert events == []
    assert hard_stop is False


def test_clustered_deadlock_debugging_is_quiet(make_gs, loads_at_turns):
    """25 loads all at T325-T326 is legitimate debugging, not scumming.

    Two distinct turns spanning 1 — below every threshold.
    """
    turns = [326 if i % 2 == 0 else 325 for i in range(25)]
    events, hard_stop = _check_save_scumming(make_gs(loads_at_turns(turns)))
    assert events == []
    assert hard_stop is False


def test_loads_clustered_at_two_turns_are_quiet(make_gs, loads_at_turns):
    """6 loads but only 2 distinct turns spanning 1 — single-point debugging."""
    turns = [200, 200, 201, 200, 201, 200]
    events, hard_stop = _check_save_scumming(make_gs(loads_at_turns(turns)))
    assert events == []
    assert hard_stop is False


def test_boot_loads_are_ignored(make_gs, loads_at_turns):
    """Loads at turn 0 happen pre-game and must not count."""
    events, hard_stop = _check_save_scumming(make_gs(loads_at_turns([0, 0, 0])))
    assert events == []
    assert hard_stop is False


def test_three_loads_spanning_ten_turns_warn(make_gs, loads_at_turns):
    events, hard_stop = _check_save_scumming(make_gs(loads_at_turns([50, 55, 62])))
    assert len(events) == 1
    assert events[0].priority == 2
    assert "SAVE SCUMMING WARNING" in events[0].message
    assert hard_stop is False


def test_five_loads_spanning_twenty_turns_warn_strongly(make_gs, loads_at_turns):
    history = loads_at_turns([30, 40, 50, 60, 70])
    events, hard_stop = _check_save_scumming(make_gs(history))
    assert len(events) == 1
    assert events[0].priority == 1
    assert "SAVE SCUMMING CRITICAL" in events[0].message
    assert hard_stop is False


def test_eight_loads_spanning_thirty_turns_abort_the_run(make_gs, loads_at_turns):
    history = loads_at_turns([30, 40, 50, 60, 70, 80, 90, 100])
    events, hard_stop = _check_save_scumming(make_gs(history))
    assert len(events) == 1
    assert events[0].priority == 1
    assert "RUN ABORTED" in events[0].message
    assert hard_stop is True


def test_observed_scumming_pattern_aborts_the_run(make_gs, loads_at_turns):
    """Gemini's actual pattern from a real run."""
    history = loads_at_turns([106, 110, 114, 116, 122, 124, 152, 160, 161, 169])
    events, hard_stop = _check_save_scumming(make_gs(history))
    assert hard_stop is True
    assert "RUN ABORTED" in events[0].message
