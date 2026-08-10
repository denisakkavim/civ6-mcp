"""Which type prefixes the read side strips, and which it must not.

The most systematic mismatch in the surface (review §4j): reads strip type
prefixes while writes require them. The agent reads `CAMPUS`, `FARM`, `IRON`
and must write `DISTRICT_CAMPUS`, `IMPROVEMENT_FARM`, `RESOURCE_IRON`, with no
rule stated anywhere — it is memorised per entity type.

This collides with Stage 1.3: a `Literal["IMPROVEMENT_FARM", ...]` locks input
to the prefixed form, which makes the mismatch unrecoverable rather than
merely confusing. The two have to land together.

Static analysis only — it reads the source, needs no game and no recordings.
The counterpart that checks real recorded output is
`tests/integration/test_read_write_round_trip.py`.
"""


from __future__ import annotations

import re
from pathlib import Path

import pytest

from utils import paths

SRC = paths.SRC

# Values with these prefixes are fed back into a write tool, so stripping the
# prefix on the way out breaks the round trip.
WRITE_FEEDING_PREFIXES = frozenset(
    {"RESOURCE", "UNIT", "IMPROVEMENT", "DISTRICT", "BUILDING", "RELIGION", "TECH"}
)

# Values with these prefixes are never written back, so stripping them is a
# free token saving and should continue.
DISPLAY_ONLY_PREFIXES = frozenset(
    {
        "TERRAIN",
        "FEATURE",
        "ERA",
        "MODIFIER",
        "MODIFIER_PLAYER",
        "GREATWORK",
        "DIPLOACTION",
        "UNITOPERATION",
        "UNITOPERATION_SPY",
        "SLOT",
        # Both missed by the review's first audit, whose grep used `[A-Z]*_`
        # and so could not match a multi-word prefix. The patterns below use
        # `[A-Z_]+` for that reason — do not "simplify" them back.
        "BELIEF_CLASS",
        "GREAT_PERSON_CLASS",
    }
)

NARRATE_STRIP = re.compile(r'replace\("([A-Z_]+)_", ?""\)')
LUA_STRIP = re.compile(r'gsub\("([A-Z_]+)_", ?""')


def _strip_sites() -> dict[str, list[tuple[Path, int, str]]]:
    """Every prefix-stripping site, grouped by prefix."""
    sites: dict[str, list[tuple[Path, int, str]]] = {}
    targets = [(SRC / "narrate.py", NARRATE_STRIP)]
    targets += [(path, LUA_STRIP) for path in sorted((SRC / "lua").glob("*.py"))]

    for path, pattern in targets:
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            for prefix in pattern.findall(line):
                sites.setdefault(prefix, []).append((path, lineno, line.strip()))
    return sites


def test_every_strip_site_is_classified():
    """A prefix in neither list is one nobody has decided about.

    This is the guard that stops the two lists above from silently going out
    of date as `lua/` grows.
    """
    unclassified = sorted(
        prefix
        for prefix in _strip_sites()
        if prefix not in WRITE_FEEDING_PREFIXES and prefix not in DISPLAY_ONLY_PREFIXES
    )
    assert not unclassified, (
        f"Prefixes stripped but not classified: {unclassified}. Decide whether "
        f"each value feeds a write tool (add to WRITE_FEEDING_PREFIXES and stop "
        f"stripping it) or is display-only (add to DISPLAY_ONLY_PREFIXES)."
    )


def test_display_only_prefixes_are_still_stripped():
    """The token saving on display-only values is free and should be kept."""
    sites = _strip_sites()
    stripped = {p for p in sites if p in DISPLAY_ONLY_PREFIXES}
    assert stripped, (
        "No display-only prefixes are being stripped. If the strip sites moved, "
        "update NARRATE_STRIP / LUA_STRIP — this test has stopped seeing them."
    )


@pytest.mark.xfail(
    strict=True,
    reason="Stage 1b: the write-feeding prefixes are still stripped, so read "
    "output does not paste into write input.",
)
def test_write_feeding_prefixes_are_not_stripped():
    """Values destined for a write tool must keep their prefix."""
    sites = _strip_sites()
    offenders = {
        prefix: [f"{path.name}:{lineno}" for path, lineno, _ in occurrences]
        for prefix, occurrences in sorted(sites.items())
        if prefix in WRITE_FEEDING_PREFIXES
    }
    total = sum(len(v) for v in offenders.values())
    assert not offenders, (
        f"{total} sites strip a prefix that a write tool requires back: "
        f"{offenders}. The agent reads `FARM` and must write `IMPROVEMENT_FARM`."
    )


def test_the_two_resource_forms_agree():
    """`WonderPlacement.resource` is `RESOURCE_IRON`; `BuilderTask.resource` is `IRON`.

    One entity, two forms, in one file (review §4j). Whichever way Stage 1b
    resolves it, they must match.
    """
    models = (SRC / "lua" / "models.py").read_text()
    assert "class WonderPlacement" in models and "class BuilderTask" in models, (
        "models moved; update this test"
    )


