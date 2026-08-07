"""Local JSONL log of MCP tool calls.

One row per tool call, appended to ``~/.civ6-mcp/logs/<session>.jsonl``.

This is the only durable record of what the agent actually did. An agent's
own transcript lives client-side, so for agent-vs-agent play nothing else
holds a single interleaved view of both players' calls. Logging must never
break a tool call, so every write failure is swallowed.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_DIR = Path.home() / ".civ6-mcp" / "logs"


class GameLogger:
    """Appends one JSONL row per tool call to a local per-session file."""

    def __init__(self) -> None:
        self.session_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self._turn: int | None = None
        self._game: str | None = None
        self._path = LOG_DIR / f"{self.session_id}.jsonl"

    def set_turn(self, turn: int) -> None:
        self._turn = turn

    def bind_game(self, civ: str, seed: int) -> None:
        self._game = f"{civ}_{seed}"

    def _write(self, entry_type: str, tool: str, **fields: Any) -> None:
        row = {
            "ts": time.time(),
            "session": self.session_id,
            "game": self._game,
            "turn": self._turn,
            "type": entry_type,
            "tool": tool,
            **fields,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except OSError:
            pass  # never let logging break a tool call

    async def log_tool_call(
        self, tool: str, params: dict[str, Any], result: str, duration_ms: int
    ) -> None:
        self._write(
            "tool_call",
            tool,
            params=params,
            result=result.split("\n", 1)[0][:200],
            duration_ms=duration_ms,
            success=not result.startswith(("Error", "ERR")),
        )

    async def log_error(self, tool: str, error: str) -> None:
        self._write("error", tool, result=error[:200], success=False)

    async def log_game_over(
        self,
        *,
        is_defeat: bool,
        winner_civ: str,
        winner_leader: str,
        victory_type: str,
        player_alive: bool,
    ) -> None:
        self._write(
            "game_over",
            "end_turn",
            result=f"{'Defeat' if is_defeat else 'Victory'}"
            f" — {winner_leader} of {winner_civ} ({victory_type})",
            outcome={
                "is_defeat": is_defeat,
                "winner_civ": winner_civ,
                "winner_leader": winner_leader,
                "victory_type": victory_type,
                "player_alive": player_alive,
            },
        )
