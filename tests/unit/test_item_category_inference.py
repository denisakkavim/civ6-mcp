"""Stage 1.4 deleted `item_type`; the category is inferred from the prefix.

`set_city_production` and `purchase_item` used to take both `item_type`
("DISTRICT") and `item_type` ("DISTRICT_CAMPUS"), so the agent stated the same
fact twice and could state it inconsistently. The identifier already carries
its category, so the tools read it off the prefix.

That makes the prefix table load-bearing: an identifier it does not recognise
must produce a message naming what was expected, not a silent miscategorisation
or an opaque failure from the game.
"""

from __future__ import annotations

import pytest

from civ_mcp.server import (
    PRODUCIBLE_CATEGORIES,
    PURCHASABLE_CATEGORIES,
    _category_from_prefix,
    _unknown_prefix_error,
)


@pytest.mark.parametrize(
    "item_type,expected",
    [
        ("UNIT_WARRIOR", "UNIT"),
        ("BUILDING_MONUMENT", "BUILDING"),
        ("DISTRICT_CAMPUS", "DISTRICT"),
        ("PROJECT_LAUNCH_EARTH_SATELLITE", "PROJECT"),
    ],
)
def test_a_producible_identifier_states_its_own_category(item_type, expected):
    assert _category_from_prefix(item_type, PRODUCIBLE_CATEGORIES) == expected


@pytest.mark.parametrize(
    "item_type,expected",
    [("UNIT_WARRIOR", "UNIT"), ("BUILDING_MONUMENT", "BUILDING")],
)
def test_a_purchasable_identifier_states_its_own_category(item_type, expected):
    assert _category_from_prefix(item_type, PURCHASABLE_CATEGORIES) == expected


def test_districts_and_projects_cannot_be_purchased():
    """The narrower table is the point: gold buys units and buildings only."""
    assert _category_from_prefix("DISTRICT_CAMPUS", PURCHASABLE_CATEGORIES) is None
    assert (
        _category_from_prefix("PROJECT_MANHATTAN_PROJECT", PURCHASABLE_CATEGORIES)
        is None
    )


@pytest.mark.parametrize(
    "item_type",
    ["CAMPUS", "campus", "IMPROVEMENT_FARM", "TECH_POTTERY", ""],
    ids=["bare", "lowercase", "wrong-family", "wrong-family-2", "empty"],
)
def test_an_unrecognised_identifier_infers_nothing(item_type):
    """Including the bare form: stripping the prefix is exactly the §4j defect."""
    assert _category_from_prefix(item_type, PRODUCIBLE_CATEGORIES) is None


def test_the_error_names_the_value_and_every_expected_prefix():
    """An agent that guessed wrong has to be able to fix it from the message."""
    message = _unknown_prefix_error("CAMPUS", PRODUCIBLE_CATEGORIES)
    assert message.startswith("Error:")
    assert "CAMPUS" in message
    for prefix in PRODUCIBLE_CATEGORIES:
        assert prefix in message
