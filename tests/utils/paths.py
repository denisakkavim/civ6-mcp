"""Every path the suite needs, resolved once.

Test modules used to compute these with `parent.parent` chains, which silently
break the moment a file moves between folders. Import from here instead.
"""

from __future__ import annotations

from pathlib import Path

TESTS = Path(__file__).resolve().parent.parent
REPO_ROOT = TESTS.parent

SRC = REPO_ROOT / "src" / "civ_mcp"
LUA = SRC / "lua"

DATA = TESTS / "data"
RECORDINGS = DATA / "recordings"
SNAPSHOTS = DATA / "snapshots"
SAVES = DATA / "saves"
