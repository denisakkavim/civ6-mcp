"""Tests for the per-turn advisor call budget in GameState.

Gemini Pro's `divine-vermil-monument-72` run died in an infinite
`get_wonder_sites` loop — 1,567 calls in a single turn. The budget cap
prevents this class of failure by short-circuiting further calls after
the hard limit.
"""

import types

import pytest

from civ_mcp.game_state import GameState


@pytest.fixture
def gs():
    """A stand-in carrying just the fields the budget check reads.

    The real `_advisor_budget_check` is bound to it so the threshold logic
    under test is the shipped one.
    """
    stub = types.SimpleNamespace()
    stub._advisor_calls_this_turn = 0
    stub._advisor_budget_warning = None
    stub.ADVISOR_BUDGET_SOFT = GameState.ADVISOR_BUDGET_SOFT
    stub.ADVISOR_BUDGET_HARD = GameState.ADVISOR_BUDGET_HARD
    stub._record_advisor_call = types.MethodType(GameState._record_advisor_call, stub)
    stub._advisor_budget_check = types.MethodType(GameState._advisor_budget_check, stub)
    return stub


def test_first_call_is_clean(gs):
    hard, soft = gs._advisor_budget_check()
    assert hard is None
    assert soft is None
    assert gs._advisor_calls_this_turn == 1


def test_under_soft_limit_gives_no_warning(gs):
    for call in range(1, 10):  # soft limit is 10
        hard, soft = gs._advisor_budget_check()
        assert hard is None, f"call {call} should not be capped"
        assert soft is None, f"call {call} should not warn"


def test_soft_warning_fires_at_the_limit(gs):
    for _ in range(9):
        gs._advisor_budget_check()
    hard, soft = gs._advisor_budget_check()  # 10th call
    assert hard is None
    assert soft is not None
    assert "ADVISOR BUDGET" in soft
    assert "10/20" in soft


def test_soft_warning_continues_in_the_warning_zone(gs):
    for _ in range(14):
        gs._advisor_budget_check()
    hard, soft = gs._advisor_budget_check()  # 15th call, still under the cap
    assert hard is None
    assert soft is not None
    assert "15/20" in soft


def test_hard_cap_fires_on_the_twenty_first_call(gs):
    for i in range(20):
        hard, _ = gs._advisor_budget_check()
        assert hard is None, f"call {i + 1} should not be hard-capped"
    hard, _ = gs._advisor_budget_check()
    assert hard is not None
    assert "ADVISOR_BUDGET_EXCEEDED" in hard
    assert "21" in hard


def test_budget_persists_until_reset(gs):
    for _ in range(25):
        gs._advisor_budget_check()
    hard, _ = gs._advisor_budget_check()
    assert hard is not None


def test_reset_clears_the_budget(gs):
    for _ in range(25):
        gs._advisor_budget_check()
    gs._advisor_calls_this_turn = 0  # as end_turn does
    hard, soft = gs._advisor_budget_check()
    assert hard is None
    assert soft is None


def test_hard_error_is_actionable(gs):
    for _ in range(20):
        gs._advisor_budget_check()
    hard, _ = gs._advisor_budget_check()  # 21st call trips the cap
    assert "limit 20" in hard
    assert "Resets next turn" in hard
    assert "ERR:" in hard


def test_soft_warning_is_informative(gs):
    for _ in range(9):
        gs._advisor_budget_check()
    _, soft = gs._advisor_budget_check()  # 10th call trips the warning
    assert "10/20" in soft
    assert "Resets next turn" in soft
