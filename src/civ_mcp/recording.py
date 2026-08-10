"""Record live FireTuner traffic into replayable recordings.

Off unless ``CIV_MCP_RECORD`` names a directory. When on, every Lua round trip
is teed to the recording for the tool call currently in flight, and the
recording is written when that call returns.

This exists so test fixtures are ground truth rather than invention. A
hand-written response line encodes a belief about what the game emits; if the
belief is wrong the test passes and the game fails. Recording removes the
belief.

Nothing here may break a tool call: every failure is swallowed, exactly as in
``logger.py``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ENV_DIR = "CIV_MCP_RECORD"
ENV_SCENARIO = "CIV_MCP_RECORD_SCENARIO"

READ = "read"
WRITE = "write"


@dataclass
class _Exchange:
    context: str
    lua: str
    lines: list[str]


@dataclass
class _InFlight:
    tool: str
    arguments: dict[str, Any]
    exchanges: list[_Exchange] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)


@dataclass
class Recorder:
    directory: Path
    scenario: str = "unknown"
    _current: _InFlight | None = None
    written: list[Path] = field(default_factory=list)

    def begin(self, tool: str, arguments: dict[str, Any]) -> None:
        self._current = _InFlight(tool=tool, arguments=dict(arguments or {}))

    def record(self, context: str, lua: str, lines: list[str]) -> None:
        if self._current is None:
            # Traffic outside a tool call — a background poller, or the
            # auto-boot verifier. Not part of any tool's contract.
            return
        self._current.exchanges.append(
            _Exchange(context=context, lua=lua, lines=list(lines))
        )

    def finish(self, result: str) -> Path | None:
        current, self._current = self._current, None
        if current is None:
            return None
        payload = {
            "tool": current.tool,
            "arguments": current.arguments,
            "scenario": self.scenario,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "notes": None,
            "result": result,
            "exchanges": [
                {"context": e.context, "lua": e.lua, "lines": e.lines}
                for e in current.exchanges
            ],
        }
        path = self.directory / self.scenario / f"{_slug(current)}.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2) + "\n")
        except OSError:
            log.warning("Could not write recording %s", path, exc_info=True)
            return None
        self.written.append(path)
        return path


_active: Recorder | None = None


def _slug(current: _InFlight) -> str:
    """A filename that distinguishes calls to the same tool by argument.

    ``unit_action`` is recorded once per verb, so the tool name alone would
    have each recording overwrite the last.
    """
    parts = [current.tool]
    for key in ("action", "mode", "work", "stance", "mission", "category"):
        value = current.arguments.get(key)
        if isinstance(value, str):
            parts.append(value)
    slug = "__".join(parts)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", slug)


def configure_from_env() -> Recorder | None:
    """Enable recording if ``CIV_MCP_RECORD`` is set. Returns the recorder."""
    directory = os.environ.get(ENV_DIR)
    if not directory:
        return None
    return enable(Path(directory), os.environ.get(ENV_SCENARIO, "unknown"))


def enable(directory: Path, scenario: str = "unknown") -> Recorder:
    global _active
    _active = Recorder(directory=directory, scenario=scenario)
    log.info("Traffic recording ON — %s (scenario=%s)", directory, scenario)
    return _active


def disable() -> None:
    global _active
    _active = None


def is_recording() -> bool:
    return _active is not None


def begin(tool: str, arguments: dict[str, Any]) -> None:
    if _active is not None:
        try:
            _active.begin(tool, arguments)
        except Exception:  # never break a tool call
            log.debug("recorder.begin failed", exc_info=True)


def record(context: str, lua: str, lines: list[str]) -> None:
    if _active is not None:
        try:
            _active.record(context, lua, lines)
        except Exception:
            log.debug("recorder.record failed", exc_info=True)


def finish(result: str) -> None:
    if _active is not None:
        try:
            _active.finish(result)
        except Exception:
            log.debug("recorder.finish failed", exc_info=True)
