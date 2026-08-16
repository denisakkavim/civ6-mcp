"""The Great Person advisor must report the game's reason, not a guess.

Two defects, and the second hid the first for as long as the tool existed.

`build_gp_advisor_query` read the Great Person's class from
`GameInfo.Units[...].GreatPersonClass`. That column does not exist, so the
read was nil for every Great Person and the query bailed with
ERR:UNKNOWN_GP_CLASS. The class comes from the unit's GreatPerson object:
`GetClass()` returns an index into `GameInfo.GreatPersonClasses`.

`get_gp_advisor` then returned None for any bail, and the tool replaced every
reason with "Is this a Great Person unit?" — so a real Great Merchant, in a
save whose capital holds a Commercial Hub, was reported as not being a Great
Person at all.
"""

import asyncio


def test_the_query_reads_the_class_from_the_great_person_object(make_game_state):
    """Not from the unit's GameInfo row, which has no such column."""
    from civ_mcp import lua as lq

    query = lq.build_gp_advisor_query(14)

    assert "GetGreatPerson()" in query
    assert "GameInfo.GreatPersonClasses[" in query
    assert "uInfo.GreatPersonClass" not in query, (
        "reading the class off the unit row is the defect this closes"
    )


def test_a_refusal_reports_the_games_reason(make_game_state):
    gs = make_game_state([["ERR:NOT_A_GREAT_PERSON"]])

    result = asyncio.run(gs.get_gp_advisor(14))

    assert result == "Error: NOT_A_GREAT_PERSON"


def test_an_unknown_class_is_named_rather_than_guessed_at(make_game_state):
    gs = make_game_state([["ERR:UNKNOWN_GP_CLASS"]])

    result = asyncio.run(gs.get_gp_advisor(14))

    assert result == "Error: UNKNOWN_GP_CLASS"


def test_a_successful_query_still_parses(make_game_state):
    gs = make_game_state(
        [
            [
                "GP_INFO|Marcus Licinius Crassus|GREAT_PERSON_CLASS_MERCHANT"
                "|DISTRICT_COMMERCIAL_HUB|45|12|3",
                "GP_CITY|Qusqu|16777216|44|11|false|1|10|0|0",
            ]
        ]
    )

    result = asyncio.run(gs.get_gp_advisor(14))

    assert result.gp_class == "GREAT_PERSON_CLASS_MERCHANT"
    assert result.target_district == "DISTRICT_COMMERCIAL_HUB"
    assert len(result.cities) == 1
