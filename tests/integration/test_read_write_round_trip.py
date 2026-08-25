"""Recorded read output must paste straight into a write call (review §4j).

The static counterpart (`tests/contract/test_type_prefix_stripping.py`) shows
the strip sites exist. This shows they actually reach the agent, in text the
agent is then expected to hand back to a write tool.

Driven by the recorded corpus, so the evidence is what the game really printed
rather than what anyone believes it prints.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from utils import recordings

# Read tool -> the write tool its type strings are meant to feed.
CONSUMERS = {
    "get_builder_tasks": "builder_work",
    "get_production_options": "set_city_production",
    "get_empire_resources": "propose_deal",
    "get_religion_spread": "found_religion",
}

# Fully-qualified identifiers: what a write tool accepts.
QUALIFIED_TYPE = re.compile(
    r"\b(?:RESOURCE|UNIT|IMPROVEMENT|DISTRICT|BUILDING|RELIGION|TECH)_[A-Z_]+\b"
)

# Values these reads actually print, taken from recorded output. Each is the
# bare form of an identifier whose write tool requires the prefixed form:
# `get_empire_resources` prints `HORSES`, `propose_deal` wants
# `RESOURCE_HORSES`; `get_builder_tasks` prints `FARM`, the improve verb wants
# `IMPROVEMENT_FARM`.
BARE_FORMS = {
    "RESOURCE_": [
        "HORSES",
        "IRON",
        "NITER",
        "COAL",
        "OIL",
        "ALUMINUM",
        "URANIUM",
        "SALT",
        "INCENSE",
        "WINE",
        "FURS",
        "IVORY",
        "SILK",
        "DYES",
        "SPICES",
        "WHEAT",
        "RICE",
        "DEER",
        "SHEEP",
        "CATTLE",
        "BANANAS",
        "STONE",
        "COPPER",
    ],
    "IMPROVEMENT_": [
        "FARM",
        "MINE",
        "QUARRY",
        "PASTURE",
        "PLANTATION",
        "CAMP",
        "FISHING_BOATS",
        "LUMBER_MILL",
    ],
}

CORPUS = recordings.available()

_BARE_CASES = [
    (scenario, name)
    for scenario, name in CORPUS
    if name.split("__")[0]
    in {"get_empire_resources", "get_builder_tasks", "get_cities"}
]

_CONSUMER_CASES = [
    (scenario, name) for scenario, name in CORPUS if name.split("__")[0] in CONSUMERS
]


def _bare_mentions(text: str) -> dict[str, list[str]]:
    """Identifiers printed without the prefix their write tool requires."""
    found: dict[str, list[str]] = {}
    for prefix, values in BARE_FORMS.items():
        for value in values:
            # The bare token, not already preceded by its prefix.
            if re.search(rf"(?<![A-Z_])(?<!{prefix}){value}\b", text):
                found.setdefault(prefix, []).append(value)
    return found


@pytest.mark.parametrize(
    "scenario,name", _BARE_CASES, ids=[f"{s}/{n}" for s, n in _BARE_CASES] or None
)
def test_reads_print_prefixed_identifiers(scenario, name):
    """No recorded read prints an identifier its write tool would reject."""
    recording = recordings.load(scenario, name)
    bare = _bare_mentions(recording.result or "")
    assert not bare, (
        f"{recording.tool} prints bare identifiers the write side requires "
        f"prefixed: {bare}"
    )


@pytest.mark.parametrize(
    "scenario,name",
    _CONSUMER_CASES,
    ids=[f"{s}/{n}" for s, n in _CONSUMER_CASES] or None,
)
def test_qualified_types_are_accepted_by_the_consuming_write_tool(scenario, name):
    """Every fully-qualified type a read prints is legal input to its write tool.

    Once Stage 1.3 types those parameters with `Literal`, this becomes the
    check that a read never suggests a value the schema refuses.
    """
    from civ_mcp.server import mcp

    recording = recordings.load(scenario, name)
    assert recording.result is not None

    tokens = set(QUALIFIED_TYPE.findall(recording.result))
    if not tokens:
        pytest.skip(f"{name} printed no fully-qualified type strings")

    consumer = CONSUMERS[recording.tool]
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    if consumer not in tools:
        pytest.skip(f"{consumer} has been renamed; update CONSUMERS")

    enums: set[str] = set()
    for spec in (tools[consumer].inputSchema or {}).get("properties", {}).values():
        for value in spec.get("enum", []) or []:
            if isinstance(value, str):
                enums.add(value)

    if not enums:
        pytest.skip(f"{consumer} has no enum-typed parameters yet (pre Stage 1.3)")

    families = {e.split("_")[0] for e in enums}
    relevant = {t for t in tokens if t.split("_")[0] in families}
    rejected = sorted(relevant - enums)
    assert not rejected, (
        f"{recording.tool} prints values that {consumer} would reject: "
        f"{rejected}. A read must not suggest something the write refuses."
    )
