"""The diplomacy parsers must build the dataclasses they claim to build.

Both `parse_deal_options_response` and `parse_test_trade_response` passed
`player_id=` to dataclasses whose field is `other_player_id`, so every call to
`get_deal_options` and `propose_deal(mode="test")` raised TypeError before it
returned. Stage 2 renamed the *tool parameter* `other_player_id` to
`player_id`, and the rename reached these two constructors, which it should
not have.

The same rename broke `DiplomacySession` and `PendingDeal` too. Those two are
worse, because they are built only when the game actually has a session or an
offered deal. The corpus never held one, so `end_turn` replayed clean for two
stages and then crashed the moment a recorded `diplomacy_action` created a
session — `end_turn` reads pending diplomacy before it ends the turn.

Nothing caught any of it. None of the four tools had a recording, and a
constructor that raises never reaches the narration a snapshot would compare.
These tests need no game and no fixture: constructing the result at all is the
thing that was broken.
"""

from civ_mcp import lua as lq


def test_deal_options_parse_returns_a_usable_result():
    result = lq.parse_deal_options_response([])

    # narrate_deal_options reads this attribute; the old code never got here.
    assert result.other_player_id == 0
    assert result.other_civ_name == ""


def test_test_trade_parse_returns_a_usable_result():
    result = lq.parse_test_trade_response([])

    assert result.other_player_id == 0
    assert result.rejected is False


def test_deal_options_parse_reads_a_real_response():
    lines = ["CIV|1|Georgia", "ECON|83|10|36|57|-2|46"]

    result = lq.parse_deal_options_response(lines)

    assert result.other_player_id == 1
    assert result.other_civ_name == "Georgia"
    assert (result.our_gold, result.their_gold) == (83, 57)


def test_a_diplomacy_session_parses():
    """`end_turn` reads these, so a raise here stops the turn from ending."""
    sessions = lq.parse_diplomacy_sessions(["SESSION|1|1|Georgia|Tamar|hello"])

    assert len(sessions) == 1
    assert sessions[0].other_player_id == 1
    assert sessions[0].other_civ_name == "Georgia"


def test_a_pending_deal_parses():
    deals = lq.parse_pending_deals_response(["DEAL|1|Georgia|Tamar"])

    assert len(deals) == 1
    assert deals[0].other_player_id == 1


def test_every_diplomacy_parser_survives_an_empty_response():
    """A constructor with a wrong keyword raises before it reads any line.

    So an empty response is enough to catch the whole class of defect, and it
    needs no knowledge of the line formats.
    """
    for parse in (
        lq.parse_deal_options_response,
        lq.parse_test_trade_response,
        lq.parse_diplomacy_sessions,
        lq.parse_pending_deals_response,
        lq.parse_diplomacy_response,
    ):
        parse([])
