"""Every tool and parameter name embedded in agent-facing text must be real.

`end_turn.py` names ~12 tools in its blocker and warning strings, `narrate.py`
another ~20 in the text it returns, and `server.py` docstrings cross-reference
tools constantly. None of that is reachable from a call graph, so a rename
leaves the strings behind and the agent is told to call something that no
longer exists — at exactly the moment a blocker fires and it most needs the
advice.

The scan reads string literals only. A name that survives the filters below is
either a live tool, a live parameter, a live enum value, or an allowlisted
token — anything else fails.
"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path

import pytest

from civ_mcp.server import mcp
from utils import paths

SRC = paths.SRC

# Files whose string literals reach the agent, either as tool descriptions or
# as returned text.
AGENT_FACING = [
    SRC / "server.py",
    SRC / "narrate.py",
    SRC / "end_turn.py",
    SRC / "game_state.py",
    # Two lookup tables mapping a notification or an end-turn blocker to the
    # tools that clear it. Stage 2 renamed five tools these named and merged
    # three more, and nothing caught it, because the scan stopped at the four
    # files above — the advice reaches the agent at exactly the moment it is
    # blocked, which is when a dead tool name costs the most.
    SRC / "lua" / "notifications.py",
]

# snake_case tokens — the shape a tool or parameter name takes.
SNAKE_CASE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

# Local variables and dataclass fields that happen to be snake_case and happen
# to appear inside f-strings. They are not references to anything the agent can
# call. Keep this list short and justified: an unexplained addition is usually
# a rename that should have updated the string instead.
INTERNAL_IDENTIFIERS = frozenset(
    {
        # f-string interpolations of local variables
        "attacker_owner",
        "civ_type_lower",
        "garrison_hp",
        "garrison_max",
        "hard_error",
        "hard_stop",
        "needs_promo",
        "newly_revealed",
        "now_at",
        "pre_hp",
        "random_seed",
        "resource_class",
        "soft_warning",
        "total_new",
        "wall_hp",
        "wall_max",
        # `propose_deal`'s parameters are named offer_*/request_* per item
        # class; the docstring refers to the pair collectively.
        "offer_items",
        "request_items",
        # Lua-side and narration-internal labels, not callable surface.
        "map_area",
        # Parser docstrings in notifications.py: the query builder they read
        # from, and a tuple field they return.
        "build_notifications_query",
        "blocking_type",
        "test_deal",
    }
)

# Stage 1.3 typed the action dispatchers with `Literal`, so their verbs are
# enum values that `_live_surface()` resolves on its own, and Stage 2.6 turned
# the last sub-label (`resolve_city_capture`) into a tool of its own. Nothing
# is allowlisted here now. Keep it that way: a verb that needs allowlisting is
# a verb the schema does not state.
UNTYPED_ACTION_VERBS: frozenset[str] = frozenset()


def _live_surface() -> tuple[set[str], set[str], set[str]]:
    """Tool names, parameter names, and enum values as the server reports them."""
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    params: set[str] = set()
    enums: set[str] = set()
    for tool in tools:
        properties = (tool.inputSchema or {}).get("properties", {})
        params |= set(properties)
        for spec in properties.values():
            for value in spec.get("enum", []) or []:
                if isinstance(value, str):
                    enums.add(value)
            # `Literal` inside a union (e.g. `int | Literal["all"]`) lands in
            # anyOf rather than on the property itself.
            for variant in spec.get("anyOf", []) or []:
                for value in variant.get("enum", []) or []:
                    if isinstance(value, str):
                        enums.add(value)
    return names, params, enums


def _referenced_identifiers(path: Path) -> dict[str, int]:
    """Every snake_case token appearing in a string literal, with its line."""
    tree = ast.parse(path.read_text())
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for token in SNAKE_CASE.findall(node.value):
                found.setdefault(token, node.lineno)
    return found


@pytest.mark.parametrize("path", AGENT_FACING, ids=lambda p: p.name)
def test_embedded_names_resolve(path):
    """No string literal names a tool or parameter that does not exist."""
    names, params, enums = _live_surface()
    known = names | params | enums | INTERNAL_IDENTIFIERS | UNTYPED_ACTION_VERBS

    unresolved = {
        token: line
        for token, line in _referenced_identifiers(path).items()
        if token not in known
    }

    assert not unresolved, (
        f"{path.name} names identifiers that are not part of the live tool "
        f"surface: {sorted(unresolved)}. Either the string is stale after a "
        f"rename (fix the string) or the token is an internal identifier "
        f"(add it to INTERNAL_IDENTIFIERS with a reason). Lines: "
        + ", ".join(f"{t}:{ln}" for t, ln in sorted(unresolved.items()))
    )


def test_allowlist_has_no_dead_entries():
    """An allowlisted token that became a real tool should leave the list.

    Without this the allowlist silently absorbs the surface: `found_city` is
    an action verb today and a tool after Stage 4, and the entry that
    legitimately covers it now would go on suppressing checks afterwards.
    """
    names, params, enums = _live_surface()
    allowlisted = INTERNAL_IDENTIFIERS | UNTYPED_ACTION_VERBS
    absorbed = sorted(allowlisted & (names | params | enums))
    assert not absorbed, (
        f"Allowlisted tokens are now part of the real surface and should be "
        f"removed from INTERNAL_IDENTIFIERS / UNTYPED_ACTION_VERBS: {absorbed}"
    )


def test_referenced_identifiers_are_actually_found():
    """Guard against the scan silently matching nothing.

    A regex or AST change that returns an empty set would make every
    assertion above vacuously pass.
    """
    tokens = _referenced_identifiers(SRC / "end_turn.py")
    assert "end_turn" in tokens
    assert len(tokens) > 5
