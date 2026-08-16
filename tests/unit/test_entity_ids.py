"""Tests for composite entity ids (stage 3.4a).

The defect these close was live in the turn-73 save: the city Wanuku and a
Warrior were both numbered 131073, because the game's GetID() is unique per
player only and carries no owner. Passing the city id to a unit tool took
`% 65536` and acted on whatever unit held that local id, then reported success.
"""

import pytest

from civ_mcp import ids


def test_the_three_fields_round_trip():
    entity = ids.encode(ids.CITY, owner=3, local=7)

    assert ids.kind_of(entity) == ids.CITY
    assert ids.owner_of(entity) == 3
    assert ids.local_of(entity) == 7


@pytest.mark.parametrize("kind", [ids.UNIT, ids.CITY])
@pytest.mark.parametrize("owner", [0, 1, 62, 255])
@pytest.mark.parametrize("local", [0, 1, 4095, 65535])
def test_fields_never_bleed_into_each_other(kind, owner, local):
    entity = ids.encode(kind, owner, local)

    assert (ids.kind_of(entity), ids.owner_of(entity), ids.local_of(entity)) == (
        kind,
        owner,
        local,
    )


def test_a_unit_and_a_city_with_the_same_local_id_no_longer_collide():
    """The exact collision found at turn 73: Wanuku and a Warrior were both 131073."""
    warrior = ids.encode(ids.UNIT, owner=0, local=1)
    wanuku = ids.encode(ids.CITY, owner=0, local=1)

    assert warrior != wanuku
    assert ids.local_of(warrior) == ids.local_of(wanuku) == 1


def test_the_same_local_id_under_two_owners_does_not_collide():
    """Player 0 and player 3 both held a unit numbered 131073 in the same save."""
    mine = ids.encode(ids.UNIT, owner=0, local=1)
    theirs = ids.encode(ids.UNIT, owner=3, local=1)

    assert mine != theirs
    assert ids.owner_of(theirs) == 3


def test_a_right_kind_id_passes_the_guard():
    unit = ids.encode(ids.UNIT, owner=0, local=5)

    assert ids.wrong_kind_error(unit, ids.UNIT, "unit_id") is None


def test_a_wrong_kind_id_is_rejected_by_name():
    city = ids.encode(ids.CITY, owner=0, local=5)

    error = ids.wrong_kind_error(city, ids.UNIT, "unit_id")

    assert error is not None
    assert "is a city id" in error
    assert "expects a unit id" in error
    # Names where the right id comes from, so the agent can fix the call.
    assert "get_units" in error


def test_the_lua_emission_formula_matches_the_python_one():
    """The Lua sites inline this arithmetic; they must agree with ids.encode.

    Lua emits `(GetID() % 65536) + GetOwner() * 65536` for a unit and the same
    plus 16777216 for a city.
    """
    raw_get_id, owner = 1179648, 2

    lua_unit = (raw_get_id % 65536) + owner * 65536
    lua_city = (raw_get_id % 65536) + owner * 65536 + 16777216

    assert lua_unit == ids.encode(ids.UNIT, owner, raw_get_id % 65536)
    assert lua_city == ids.encode(ids.CITY, owner, raw_get_id % 65536)


class _PositionConnection:
    """Answers the city-position query and remembers the Lua it was given."""

    def __init__(self, lines):
        self._lines = lines
        self.reads: list[str] = []

    async def execute_read(self, lua):
        self.reads.append(lua)
        return self._lines


def _game_state(conn):
    from civ_mcp.game_state import GameState

    gs = GameState.__new__(GameState)
    gs.conn = conn
    return gs


def test_resolving_a_foreign_city_keeps_both_halves_of_the_id():
    """The load-bearing detail of 3.4c.

    A foreign city is only findable through the player that holds it, so the
    resolver must not reduce the id to its low bits the way own-city tools do.
    """
    import asyncio

    conn = _PositionConnection(["CITYPOS|53|12|Wolin"])
    city = ids.encode(ids.CITY, owner=4, local=0)

    result = asyncio.run(_game_state(conn).resolve_city_position(city))

    assert result == (53, 12, "Wolin")
    # Owner 4 reached the query; a blanket `% 65536` would have sent player 0.
    assert "Players[4]" in conn.reads[0]
    assert "FindID(0)" in conn.reads[0]


def test_an_unknown_city_id_is_reported_not_guessed():
    import asyncio

    conn = _PositionConnection(["ERR:NO_CITY|player 4 has no city 9"])
    city = ids.encode(ids.CITY, owner=4, local=9)

    result = asyncio.run(_game_state(conn).resolve_city_position(city))

    assert "CITY_NOT_FOUND" in result
    # Names the recycling caveat, so a stale id reads as stale rather than wrong.
    assert "captured" in result
