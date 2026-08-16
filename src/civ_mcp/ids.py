"""Composite entity ids: one integer that says what it is and who owns it.

The game's own ``GetID()`` is unique per player only — at turn 73 of the test
save, player 0 and player 3 both hold a unit numbered 131073, and every
player's first city is numbered 65536. Worse, a unit id and a city id can be
the same integer: in that same save the city Wanuku and a Warrior are both
131073. Passing the city id to a unit tool took ``% 65536`` and acted on
whatever unit held that local id, then reported success.

So every id the agent sees is re-encoded:

    id = local + owner * 65536 + kind * 16777216

    local = id % 65536          the game's per-player id
    owner = (id >> 16) & 0xFF   which player
    kind  = id >> 24            0 unit, 1 city

Owner never approaches 256, so the three fields never collide.

Two classes of tool read these back, and the difference is load-bearing:

- Own-entity tools (production, focus, governors, every unit order) take
  ``local_of`` and discard the owner, because their Lua paths assume the local
  player.
- Foreign-capable tools must keep both halves and resolve ``(owner, local)``
  to a position. A blanket ``local_of`` there throws away exactly what makes a
  foreign id resolvable.

The raw ``GetID()`` is never used directly: its high bits are engine-assigned
and carry no owner, so adding ``owner * 65536`` on top would make the owner
unrecoverable.
"""

from __future__ import annotations

UNIT = 0
CITY = 1

_LOCAL_BITS = 16
_OWNER_BITS = 8
_LOCAL_MASK = (1 << _LOCAL_BITS) - 1  # 0xFFFF
_OWNER_MASK = (1 << _OWNER_BITS) - 1  # 0xFF
_OWNER_SHIFT = _LOCAL_BITS  # 16
_KIND_SHIFT = _LOCAL_BITS + _OWNER_BITS  # 24

_KIND_NAMES = {UNIT: "unit", CITY: "city"}


def encode(kind: int, owner: int, local: int) -> int:
    """Build a composite id from its three parts."""
    return (
        (kind << _KIND_SHIFT)
        | ((owner & _OWNER_MASK) << _OWNER_SHIFT)
        | (local & _LOCAL_MASK)
    )


def local_of(entity_id: int) -> int:
    """The game's per-player id — what the Lua layer looks entities up by."""
    return entity_id & _LOCAL_MASK


def owner_of(entity_id: int) -> int:
    """Which player owns the entity."""
    return (entity_id >> _OWNER_SHIFT) & _OWNER_MASK


def kind_of(entity_id: int) -> int:
    """UNIT or CITY."""
    return entity_id >> _KIND_SHIFT


def kind_name(kind: int) -> str:
    return _KIND_NAMES.get(kind, f"kind {kind}")


def wrong_kind_error(entity_id: int, expected: int, parameter: str) -> str | None:
    """Return an error naming the mismatch, or None when the id is the right kind.

    Rejecting by name is the whole point of the type tag: without it, a city id
    handed to a unit tool is not a rejected call but a silently wrong one.
    """
    actual = kind_of(entity_id)
    if actual == expected:
        return None
    return (
        f"Error: WRONG_ID_TYPE|{entity_id} is a {kind_name(actual)} id, but"
        f" {parameter} expects a {kind_name(expected)} id."
        f" Unit ids come from get_units, city ids from get_cities."
    )
