#!/usr/bin/env python3
"""Record game traffic into replayable recordings.

Drives the MCP server through a scripted sequence of tool calls against a
running Civ 6, teeing every Lua round trip into
``tests/data/recordings/<scenario>/``. Those recordings are what let `pytest`
exercise the whole stack offline.

    uv run python scripts/install_saves.py                    # once
    # launch Civ 6 with EnableTuner=1 and load 0T_TURN37_INCA
    uv run python scripts/record_game_traffic.py --scenario turn37

**Record the dispatcher captures before Stage 4 splits them.** `unit_action`,
`city_action`, `spy_action` and `skip_remaining_units` disappear in that stage;
once they are gone there is no way to demonstrate that the eleven replacements
behave like what they replaced.

Write calls mutate the game. Reload the save between runs if you care about
comparability, and prefer `--reads-only` when you only need the read corpus.
"""

from __future__ import annotations

import asyncio
import os
import sys
from enum import Enum
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

RECORDING_DIR = ROOT / "tests" / "data" / "recordings"

app = typer.Typer(add_completion=False, help=__doc__)


# Every entry is (tool, arguments, mutates). Arguments that need a live id are
# filled in at runtime by `_resolve`.
READ_PLAN: list[tuple[str, dict, bool]] = [
    ("get_game_overview", {}, False),
    ("get_units", {}, False),
    ("get_cities", {}, False),
    ("get_city_production", {"city_id": "$CITY"}, False),
    ("get_map_area", {"center_x": "$CITY_X", "center_y": "$CITY_Y", "radius": 2}, False),
    ("get_empire_resources", {}, False),
    ("get_builder_tasks", {}, False),
    ("get_strategic_map", {}, False),
    ("get_diplomacy", {}, False),
    ("get_tech_civics", {}, False),
    ("get_policies", {}, False),
    ("get_notifications", {}, False),
    ("get_governors", {}, False),
    ("get_city_states", {}, False),
    ("get_great_people", {}, False),
    ("get_trade_routes", {}, False),
    ("get_trade_destinations", {"unit_id": "$UNIT"}, False),
    ("get_victory_progress", {}, False),
    ("get_religion_spread", {}, False),
    ("get_pantheon_beliefs", {}, False),
    ("get_religion_beliefs", {}, False),
    ("get_dedications", {}, False),
    ("get_world_congress", {}, False),
    ("get_pending_trades", {}, False),
    ("get_pending_diplomacy", {}, False),
    ("get_spies", {}, False),
    ("get_purchasable_tiles", {"city_id": "$CITY"}, False),
    ("get_district_advisor", {"city_id": "$CITY", "district_type": "$DISTRICT"}, False),
    ("get_wonder_advisor", {"city_id": "$CITY", "wonder_name": "$WONDER"}, False),
    ("get_settle_advisor", {"unit_id": "$SETTLER"}, False),
    ("get_global_settle_advisor", {}, False),
    ("get_unit_promotions", {"unit_id": "$UNIT"}, False),
    ("get_pathing_estimate", {"unit_id": "$UNIT", "target_x": "$CITY_X", "target_y": "$CITY_Y"}, False),
]

# The dispatchers Stage 4 deletes. One recording per verb, so the replacements
# can be checked against recorded behaviour rather than against memory.
#
# Every verb gets its **own unit**. Run against a single unit they interfere:
# `fortify` and `skip` end that unit's turn, so a later `move` records
# `NO_MOVES` and the recording captures a failure instead of the behaviour.
# `move` runs first for the same reason.
#
# `heal` needs a damaged unit and `spy_action` needs a spy; neither exists in
# every save, so both are omitted here and recorded from a scenario that has
# them (see --show-plan). A recording of an error is not a fixture.
DISPATCHER_PLAN: list[tuple[str, dict, bool]] = [
    (
        "unit_action",
        {"unit_id": "$UNIT_0", "action": "move", "target_x": "$MOVE_X", "target_y": "$MOVE_Y"},
        True,
    ),
    ("unit_action", {"unit_id": "$UNIT_1", "action": "fortify"}, True),
    ("unit_action", {"unit_id": "$UNIT_2", "action": "skip"}, True),
    # Sleep is refused for a unit that has already fortified or alerted, which
    # most military units in a mid-game save have. A builder can always sleep.
    ("unit_action", {"unit_id": "$BUILDER", "action": "sleep"}, True),
    ("unit_action", {"unit_id": "$UNIT_3", "action": "alert"}, True),
    ("unit_action", {"unit_id": "$UNIT_4", "action": "automate"}, True),
    # Last: it acts on whatever is still unmoved, so it has to follow the rest.
    ("skip_remaining_units", {}, True),
]

WRITE_PLAN: list[tuple[str, dict, bool]] = [
    ("set_city_production", {"city_id": "$CITY", "item_type": "UNIT", "item_name": "$UNIT_TYPE"}, True),
    ("set_research", {"tech_or_civic": "$TECH", "category": "tech"}, True),
    ("set_city_focus", {"city_id": "$CITY", "focus": "production"}, True),
    ("end_turn", {}, True),
]


async def _resolve(client, plan):
    """Substitute $PLACEHOLDERs from a live read of the game."""
    context: dict[str, object] = {}

    units_text = await _call(client, "get_units", {})
    cities_text = await _call(client, "get_cities", {})

    unit_ids = _ids(units_text, r"id[=: ](\d+)")
    city_ids = _ids(cities_text, r"id[=: ](\d+)")
    coords = _coords(cities_text)
    unit_coords = _coords(units_text)

    if not unit_ids:
        print("! No units found — is a game actually loaded?", file=sys.stderr)
    context["$UNIT"] = unit_ids[0] if unit_ids else 0
    context["$SETTLER"] = _first_matching(units_text, "SETTLER") or context["$UNIT"]
    context["$SPY"] = _first_matching(units_text, "SPY") or context["$UNIT"]
    context["$CITY"] = city_ids[0] if city_ids else 0
    context["$CITY_X"], context["$CITY_Y"] = coords[0] if coords else (0, 0)

    # One unit per dispatcher verb. The builder is addressed by role (only it
    # can reliably sleep) and therefore excluded from the numbered slots, or
    # two verbs would land on it and interfere.
    context["$BUILDER"] = _first_matching(units_text, "BUILDER")
    others = [uid for uid in unit_ids if uid != context["$BUILDER"]]
    if context["$BUILDER"] is None:
        context["$BUILDER"] = others[0] if others else 0
    for slot in range(5):
        context[f"$UNIT_{slot}"] = (
            others[slot] if slot < len(others) else (others[-1] if others else 0)
        )
    if len(others) < 5:
        print(
            f"! Only {len(others)} non-builder units — dispatcher verbs will "
            f"share units and may record interference errors.",
            file=sys.stderr,
        )

    # Move one tile onto a neighbour that is actually passable. Stepping
    # blindly east records `BLOCKED (impassable mountain)`, which is a real
    # result but a poor fixture for the move path.
    unit_x, unit_y = unit_coords[0] if unit_coords else (0, 0)
    context["$MOVE_X"], context["$MOVE_Y"] = await _passable_neighbour(
        client, unit_x, unit_y
    )

    # Districts and wonders have to come from what this city can actually
    # build right now — a hardcoded DISTRICT_CAMPUS records an error recording
    # in any game that has not researched Writing yet.
    production = await _call(client, "get_city_production", {"city_id": context["$CITY"]})
    context["$DISTRICT"] = _first_token(production, "DISTRICT_") or "DISTRICT_ENCAMPMENT"
    context["$WONDER"] = _first_wonder(production) or "BUILDING_PYRAMIDS"
    context["$UNIT_TYPE"] = _first_token(production, "UNIT_") or "UNIT_WARRIOR"

    research = await _call(client, "get_tech_civics", {})
    context["$TECH"] = _first_token(research, "TECH_") or "TECH_MINING"

    print("Resolved:", {k: v for k, v in context.items()})

    resolved = []
    for tool, arguments, mutates in plan:
        filled = {
            key: context.get(value, value) if isinstance(value, str) else value
            for key, value in arguments.items()
        }
        resolved.append((tool, filled, mutates))
    return resolved


def _ids(text: str, pattern: str) -> list[int]:
    import re

    return [int(m) for m in re.findall(pattern, text)]


def _first_matching(text: str, needle: str) -> int | None:
    import re

    for line in text.splitlines():
        if needle in line.upper():
            match = re.search(r"id[=: ](\d+)", line)
            if match:
                return int(match.group(1))
    return None


def _coords(text: str) -> list[tuple[int, int]]:
    """Positions as the narrator prints them: `at (45,12)`."""
    import re

    return [(int(x), int(y)) for x, y in re.findall(r"\((\d+),\s*(\d+)\)", text)]


IMPASSABLE = ("MOUNTAIN", "OCEAN", "CLIFF")


async def _passable_neighbour(client, x: int, y: int) -> tuple[int, int]:
    """An adjacent tile the unit can actually enter, or one step east."""
    import re

    area = await _call(
        client, "get_map_area", {"center_x": x, "center_y": y, "radius": 1}
    )
    for line in area.splitlines():
        match = re.match(r"\s*\((\d+),(\d+)\):\s*(.*)", line)
        if not match:
            continue
        tx, ty, rest = int(match.group(1)), int(match.group(2)), match.group(3).upper()
        if (tx, ty) == (x, y):
            continue
        if any(word in rest for word in IMPASSABLE):
            continue
        if "[MY:" in rest or "[ENEMY" in rest:  # occupied
            continue
        return tx, ty
    return x + 1, y


def _first_token(text: str, prefix: str) -> str | None:
    """First fully-qualified identifier with this prefix."""
    import re

    match = re.search(rf"\b{prefix}[A-Z_]+\b", text)
    return match.group(0) if match else None


def _first_wonder(production: str) -> str | None:
    """First BUILDING_* under the Wonders heading, if there is one."""
    import re

    section = re.split(r"^Wonders:", production, flags=re.M)
    if len(section) < 2:
        return None
    return _first_token(section[1], "BUILDING_")


async def _call(client, tool: str, arguments: dict) -> str:
    result = await client.call_tool(tool, arguments)
    return "".join(b.text for b in result.content if getattr(b, "text", None))


async def _record(scenario: str, plan, dry_run: bool) -> None:
    from mcp.shared.memory import create_connected_server_and_client_session

    import civ_mcp.recording as recording
    import civ_mcp.server as server
    from civ_mcp.connection import GameConnection

    # Deliberately NOT via CIV_MCP_RECORD. The server reads that variable in
    # `lifespan` and switches recording on immediately — before the resolver
    # below has run — which silently overwrites the recordings the plan is
    # about to make, from whatever state the game is in at resolve time. It
    # also made `--dry-run` write files. Enable explicitly instead, after
    # resolution, so the ordering is visible here rather than implied by an
    # environment variable read somewhere else.
    os.environ.pop("CIV_MCP_RECORD", None)

    # Background services must be off while recording. Both poll the
    # connection on a timer, and a poll that lands mid-tool-call is recorded
    # into that tool's recording — traffic the tool never issued, which then
    # desynchronises every replay.
    with server.testing_overrides(
        connection_factory=GameConnection, background_services=False
    ):
        async with create_connected_server_and_client_session(
            server.mcp, raise_exceptions=False
        ) as client:
            # The resolver calls real tools (`get_units`, `get_cities`,
            # `get_city_production`, …) to fill in live ids. Those calls must
            # not be recorded.
            resolved = await _resolve(client, plan)

            if dry_run:
                for tool, arguments, _ in resolved:
                    print(f"  would record {tool}({_short(arguments)})")
                print(f"\nDry run — nothing written.")
                return

            recording.enable(RECORDING_DIR, scenario)
            for tool, arguments, _ in resolved:
                text = await _call(client, tool, arguments)
                first = text.split("\n", 1)[0][:90]
                flag = "!" if text.startswith("Error") else " "
                print(f" {flag} {tool}({_short(arguments)}) -> {first}")

            written = recording._active.written if recording._active else []
            print(f"\nWrote {len(written)} recordings to {RECORDING_DIR / scenario}")


def _short(arguments: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in arguments.items())[:60]


class Subset(str, Enum):
    """Which slice of the plan to record."""

    all = "all"
    reads = "reads"
    dispatchers = "dispatchers"
    writes = "writes"


PLANS = {
    Subset.reads: lambda: READ_PLAN,
    Subset.dispatchers: lambda: DISPATCHER_PLAN,
    Subset.writes: lambda: WRITE_PLAN,
    Subset.all: lambda: READ_PLAN + DISPATCHER_PLAN + WRITE_PLAN,
}


@app.command()
def main(
    scenario: str = typer.Option(
        "turn37",
        help="Subdirectory to write into. Name it after the save you loaded.",
    ),
    subset: Subset = typer.Option(
        Subset.all,
        help="Which calls to record. 'dispatchers' covers the tools Stage 4 deletes.",
    ),
    show_plan: bool = typer.Option(
        False, "--show-plan", help="Print the calls that would be recorded and exit."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Resolve live ids but record nothing."
    ),
) -> None:
    """Record a scenario's worth of tool calls from a running game."""
    plan = PLANS[subset]()

    if show_plan:
        for tool, arguments, mutates in plan:
            typer.echo(f"  {'W' if mutates else 'R'}  {tool}({_short(arguments)})")
        typer.echo(f"\n{len(plan)} calls")
        raise typer.Exit()

    asyncio.run(_record(scenario, plan, dry_run))


if __name__ == "__main__":
    app()
