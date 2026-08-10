"""Snapshot helpers.

A snapshot records what the server currently produces, so a change shows up
as a reviewable diff rather than as nothing at all. Regenerate deliberately:

    uv run pytest --update-snapshots

Never regenerate to make a red suite green without reading the diff first —
that is the failure mode snapshots exist to prevent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from utils import paths

SNAPSHOT_DIR = paths.SNAPSHOTS

# Set by the `--update-snapshots` flag in conftest; the env var is the escape
# hatch for running a single test outside pytest.
UPDATE_ENV = "CIV_MCP_UPDATE_SNAPSHOTS"


def _updating() -> bool:
    return os.environ.get(UPDATE_ENV) == "1"


def check_text(name: str, actual: str) -> None:
    """Compare text against ``tests/data/snapshots/<name>``, or rewrite it."""
    path = SNAPSHOT_DIR / name
    if _updating():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
        return
    if not path.exists():
        raise AssertionError(
            f"No snapshot at {path}. Create it with "
            f"`uv run pytest --update-snapshots` after checking the output is right."
        )
    expected = path.read_text()
    assert actual == expected, _text_diff(path, expected, actual)


def check_json(name: str, actual: Any) -> None:
    """Compare a JSON-serialisable value against ``tests/data/snapshots/<name>``."""
    path = SNAPSHOT_DIR / name
    rendered = json.dumps(actual, indent=2, sort_keys=True) + "\n"
    if _updating():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
        return
    if not path.exists():
        raise AssertionError(
            f"No snapshot at {path}. Create it with "
            f"`uv run pytest --update-snapshots` after checking the output is right."
        )
    expected = json.loads(path.read_text())
    assert actual == expected, _json_diff(path, expected, actual)


def _text_diff(path: Path, expected: str, actual: str) -> str:
    import difflib

    diff = difflib.unified_diff(
        expected.splitlines(),
        actual.splitlines(),
        fromfile=f"{path.name} (recorded)",
        tofile=f"{path.name} (now)",
        lineterm="",
    )
    return (
        f"Output changed against snapshot {path}.\n"
        f"If the change is intended, re-run with --update-snapshots and review "
        f"the diff in your commit.\n\n" + "\n".join(diff)
    )


def _json_diff(path: Path, expected: Any, actual: Any) -> str:
    lines = [
        f"Snapshot changed: {path}.",
        "If the change is intended, re-run with --update-snapshots and review "
        "the diff in your commit.",
        "",
    ]
    if isinstance(expected, dict) and isinstance(actual, dict):
        removed = sorted(set(expected) - set(actual))
        added = sorted(set(actual) - set(expected))
        changed = sorted(
            key for key in set(expected) & set(actual) if expected[key] != actual[key]
        )
        if removed:
            lines.append(f"  removed: {removed}")
        if added:
            lines.append(f"  added:   {added}")
        if changed:
            lines.append(f"  changed: {changed}")
    return "\n".join(lines)
