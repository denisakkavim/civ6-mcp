"""Recorded FireTuner traffic, replayed through the whole stack.

A recording is one captured run of one tool: the ordered exchanges it issued
against the game, plus the text it returned. Replaying one drives the real
tool — argument validation, tool body, ``GameState``, the ``lua/`` builders,
the parsers, ``narrate.py`` — with the game replaced by a tape.

**Matching is positional, not by Lua text.** Exchanges are popped in recorded
order per context, exactly as ``StubConnection`` does. This is deliberate.
Stages 1b and 3.4 of the refactor change the Lua that gets sent — prefix
stripping, composite city ids — and a recording keyed on Lua text would break
on every such edit, training whoever runs the suite to regenerate without
reading. Keyed positionally, a recording survives a Lua edit and breaks only
when the *number or order* of queries changes, which is a real behaviour
change that deserves a look. Stage 3.1's implicit advisor call inside
``set_city_production`` is exactly that case.

The Lua is recorded anyway and reported on mismatch, so a failure says which
query went unanswered rather than ``IndexError: pop from empty list``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from utils import paths

RECORDING_DIR = paths.RECORDINGS

READ = "read"
WRITE = "write"


class RecordingExhausted(AssertionError):
    """The tool issued more queries than the recording captured."""


@dataclass
class Exchange:
    """One Lua round trip: what was sent, and what the game said back."""

    context: str  # READ (gamecore) or WRITE (ingame)
    lua: str
    lines: list[str]

    def to_json(self) -> dict[str, Any]:
        return {"context": self.context, "lua": self.lua, "lines": self.lines}

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Exchange:
        return cls(context=raw["context"], lua=raw["lua"], lines=list(raw["lines"]))


@dataclass
class Recording:
    """One captured tool invocation."""

    tool: str
    arguments: dict[str, Any]
    exchanges: list[Exchange]
    result: str | None = None
    scenario: str | None = None
    recorded_at: str | None = None
    notes: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "arguments": self.arguments,
            "scenario": self.scenario,
            "recorded_at": self.recorded_at,
            "notes": self.notes,
            "result": self.result,
            "exchanges": [e.to_json() for e in self.exchanges],
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Recording:
        return cls(
            tool=raw["tool"],
            arguments=raw.get("arguments", {}),
            exchanges=[Exchange.from_json(e) for e in raw["exchanges"]],
            result=raw.get("result"),
            scenario=raw.get("scenario"),
            recorded_at=raw.get("recorded_at"),
            notes=raw.get("notes"),
        )

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=2) + "\n")

    @classmethod
    def load(cls, path: Path) -> Recording:
        return cls.from_json(json.loads(path.read_text()))


def load(scenario: str, name: str) -> Recording:
    """Load ``tests/data/recordings/<scenario>/<name>.json``."""
    path = RECORDING_DIR / scenario / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No recording at {path}. Record one with "
            f"`uv run python scripts/record_game_traffic.py` "
            f"against a running game."
        )
    return Recording.load(path)


def available() -> list[tuple[str, str]]:
    """Every (scenario, name) pair on disk, sorted."""
    if not RECORDING_DIR.exists():
        return []
    return sorted(
        (path.parent.name, path.stem)
        for path in RECORDING_DIR.glob("*/*.json")
    )


@dataclass
class ReplayConnection:
    """A ``GameConnection`` stand-in that replays a recording.

    Separate queues per context: a tool's reads and writes interleave in ways
    that depend on control flow (``get_units`` issues an optional threat scan
    inside a ``try``), so a single queue would desynchronise whenever an
    optional branch changed. Per-context ordering is the invariant that
    actually holds.
    """

    recording: Recording
    strict: bool = True
    issued: list[Exchange] = field(default_factory=list)

    # Present so code that inspects the connection (recovery paths, the
    # auto-boot verifier) sees a connected game rather than a main menu.
    gamecore_index: int = 0
    ingame_index: int = 1

    def __post_init__(self) -> None:
        self._queues: dict[str, list[Exchange]] = {READ: [], WRITE: []}
        for exchange in self.recording.exchanges:
            self._queues[exchange.context].append(exchange)

    @property
    def is_connected(self) -> bool:
        return True

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def reconnect(self) -> None:
        return None

    async def ensure_connected(self) -> None:
        return None

    async def execute_read(self, lua: str, timeout: float = 5.0) -> list[str]:
        return self._next(READ, lua)

    async def execute_write(self, lua: str, timeout: float = 5.0) -> list[str]:
        return self._next(WRITE, lua)

    async def execute_in_state(
        self, state_index: int, lua: str, timeout: float = 5.0
    ) -> list[str]:
        context = READ if state_index == self.gamecore_index else WRITE
        return self._next(context, lua)

    def _next(self, context: str, lua: str) -> list[str]:
        queue = self._queues[context]
        if not queue:
            if not self.strict:
                return []
            raise RecordingExhausted(
                f"{self.recording.tool}: the tool issued more {context} queries "
                f"than the recording recorded ({len(self._counted(context))} "
                f"available). The unanswered query begins:\n"
                f"  {lua[:400]}\n"
                f"If the tool legitimately queries more than it used to, "
                f"re-record the recording against a live game; if not, this is "
                f"the bug."
            )
        exchange = queue.pop(0)
        self.issued.append(Exchange(context=context, lua=lua, lines=exchange.lines))
        return list(exchange.lines)

    def _counted(self, context: str) -> list[Exchange]:
        return [e for e in self.recording.exchanges if e.context == context]

    def unused(self) -> list[Exchange]:
        """Exchanges the recording held that the tool never asked for."""
        return [e for queue in self._queues.values() for e in queue]
