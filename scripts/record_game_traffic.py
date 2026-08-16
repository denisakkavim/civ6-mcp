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
`city_attack`, `spy_action` and `skip_remaining_units` disappear in that stage;
once they are gone there is no way to demonstrate that the eleven replacements
behave like what they replaced.

Write calls mutate the game. Reload the save between runs if you care about
comparability, and pass `--subset reads` when you only need the read corpus.

A verb whose preconditions the loaded save cannot meet is skipped, with the
reason printed. So no single save has to cover everything: record each scenario
from its own save and the corpus accumulates verbs across them.
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
    ("get_production_options", {"city_id": "$CITY"}, False),
    (
        "get_map_area",
        {"center_x": "$CITY_X", "center_y": "$CITY_Y", "radius": 2},
        False,
    ),
    # The entity-addressed half of the same read. Stage 3 split the two so
    # each states its requirement in the schema.
    ("get_map_around", {"entity_id": "$CITY", "radius": 2}, False),
    ("get_empire_resources", {}, False),
    ("get_builder_tasks", {}, False),
    ("get_exploration_status", {}, False),
    ("get_diplomacy", {}, False),
    ("get_research_options", {}, False),
    ("get_policies", {}, False),
    ("get_notifications", {}, False),
    ("get_governors", {}, False),
    ("get_city_states", {}, False),
    ("get_great_people", {}, False),
    ("get_trade_routes", {}, False),
    ("get_trade_destinations", {"unit_id": "$UNIT"}, False),
    ("get_victory_progress", {}, False),
    ("get_religion_spread", {}, False),
    ("get_belief_options", {}, False),
    ("get_dedications", {}, False),
    ("get_world_congress", {}, False),
    ("get_pending_deals", {}, False),
    ("get_pending_diplomacy", {}, False),
    ("get_spies", {}, False),
    ("get_purchasable_tiles", {"city_id": "$CITY"}, False),
    ("get_district_sites", {"city_id": "$CITY", "district_type": "$DISTRICT"}, False),
    ("get_wonder_sites", {"city_id": "$CITY", "wonder_type": "$WONDER"}, False),
    # Any unit will do — this asks where that unit could settle, and a save
    # without a settler should still record the read.
    ("get_settle_sites_near_unit", {"unit_id": "$UNIT"}, False),
    ("get_settle_sites_on_map", {}, False),
    ("get_unit_promotions", {"unit_id": "$UNIT"}, False),
    (
        "get_pathing_estimate",
        {"unit_id": "$UNIT", "target_x": "$CITY_X", "target_y": "$CITY_Y"},
        False,
    ),
]

# The dispatchers Stage 4 deletes. One recording per verb, so the replacements
# can be checked against recorded behaviour rather than against memory.
#
# Every verb gets its **own unit**. Run against a single unit they interfere:
# `fortify` and `skip` end that unit's turn, so a later `move` records
# `NO_MOVES` and the recording captures a failure instead of the behaviour.
# `move` runs first for the same reason.
#
# Verbs below the divider need a unit or a tile state no single save is
# guaranteed to have — a settler, an idle trader, a damaged unit, a builder
# standing somewhere it can actually build. Their placeholders resolve to None
# when the game cannot supply one, and `_resolve` drops the entry with a
# printed reason. That is deliberate: a recording of an error is not a fixture,
# so a verb is captured from whichever scenario can satisfy it and skipped
# everywhere else.
DISPATCHER_PLAN: list[tuple[str, dict, bool]] = [
    (
        "unit_action",
        {
            "unit_id": "$UNIT_0",
            "action": "move",
            "target_x": "$MOVE_X",
            "target_y": "$MOVE_Y",
        },
        True,
    ),
    ("unit_action", {"unit_id": "$UNIT_1", "action": "fortify"}, True),
    ("unit_action", {"unit_id": "$UNIT_2", "action": "skip"}, True),
    ("unit_action", {"unit_id": "$UNIT_3", "action": "alert"}, True),
    ("unit_action", {"unit_id": "$UNIT_4", "action": "automate"}, True),
    # --- conditional on what the save contains ---------------------------
    # `improve` has to run before anything that ends the builder's turn, and it
    # takes no coordinates: the builder acts on the tile it is standing on, so
    # the resolver only offers this verb when `get_units` reports a `Can build`
    # line for that builder. (Stage 3.2 adds move-then-act; until then, being
    # on the tile is the precondition.)
    (
        "unit_action",
        {
            "unit_id": "$IMPROVE_UNIT",
            "action": "improve",
            "improvement_type": "$IMPROVEMENT",
        },
        True,
    ),
    # Sleep is refused for a unit that has already fortified or alerted, which
    # most military units in a mid-game save have — and the builder above is
    # busy improving. So this wants a *second* builder, and skips otherwise;
    # the turn37 corpus already holds a sleep recording.
    ("unit_action", {"unit_id": "$SLEEPER", "action": "sleep"}, True),
    ("unit_action", {"unit_id": "$DAMAGED", "action": "heal"}, True),
    # `activate` also needs the Great Person to be standing on its matching
    # district, which nothing in `get_units` reports. If it is not, this
    # records an error — `test_recording_captured_a_successful_call` fails on
    # it by name, which is the signal to delete that one capture.
    ("unit_action", {"unit_id": "$GREAT_PERSON", "action": "activate"}, True),
    ("unit_action", {"unit_id": "$RELIGIOUS_UNIT", "action": "spread_religion"}, True),
    # Teleport first: it needs an idle trader, and establishing a route
    # consumes exactly that idleness.
    (
        "unit_action",
        {
            "unit_id": "$IDLE_TRADER",
            "action": "teleport",
            "target_x": "$OTHER_CITY_X",
            "target_y": "$OTHER_CITY_Y",
        },
        True,
    ),
    (
        "spy_action",
        {
            "unit_id": "$SPY",
            "action": "travel",
            "target_x": "$OTHER_CITY_X",
            "target_y": "$OTHER_CITY_Y",
        },
        True,
    ),
    # Founding reshapes ownership and city lists, so it follows every read-like
    # verb above.
    ("unit_action", {"unit_id": "$SETTLER", "action": "found_city"}, True),
    # Last: it acts on whatever is still unmoved, so it has to follow the rest.
    ("skip_remaining_units", {}, True),
    # After skip_remaining_units, because it destroys its unit. Harmless to the
    # fixture — every session reloads the save first.
    ("unit_action", {"unit_id": "$EXPENDABLE", "action": "delete"}, True),
]

WRITE_PLAN: list[tuple[str, dict, bool]] = [
    ("set_city_production", {"city_id": "$CITY", "item_type": "$UNIT_TYPE"}, True),
    ("set_tech", {"tech_type": "$TECH"}, True),
    ("set_city_focus", {"city_id": "$CITY", "focus": "production"}, True),
    ("end_turn", {}, True),
]


# Placeholders that name a unit by its role. `delete` must not be handed any of
# them, and they are strict: a save without the unit skips the verb rather than
# aiming it at whatever unit happens to be first.
ROLE_PLACEHOLDERS = frozenset(
    {
        "$SETTLER",
        "$SPY",
        "$DAMAGED",
        "$GREAT_PERSON",
        "$RELIGIOUS_UNIT",
        "$IDLE_TRADER",
        "$IMPROVE_UNIT",
        "$BUILDER",
        "$SLEEPER",
    }
)


async def _resolve(client, plan):
    """Substitute $PLACEHOLDERs from a live read of the game."""
    context: dict[str, object] = {}

    units_text = await _call(client, "get_units", {})
    cities_text = await _call(client, "get_cities", {})

    unit_ids = _ids(units_text, r"id[=: ](\d+)")
    city_ids = _ids(cities_text, r"id[=: ](\d+)")
    coords = _city_positions(cities_text)
    unit_coords = _coords(units_text)

    if not unit_ids:
        print("! No units found — is a game actually loaded?", file=sys.stderr)
    context["$UNIT"] = unit_ids[0] if unit_ids else 0
    context["$CITY"] = city_ids[0] if city_ids else 0
    context["$CITY_X"], context["$CITY_Y"] = coords[0] if coords else (0, 0)

    # A second city, for the verbs that send a unit somewhere. None when the
    # empire has only one — teleport and spy travel then skip.
    if len(coords) > 1:
        context["$OTHER_CITY_X"], context["$OTHER_CITY_Y"] = coords[1]
    else:
        context["$OTHER_CITY_X"], context["$OTHER_CITY_Y"] = None, None

    # Strict: these gate a *write*, so a miss must skip the verb rather than
    # aim it at whatever unit happens to be first. (The settle-site read below
    # is happy with any unit and asks for `$UNIT`.)
    context["$SETTLER"] = _first_matching(units_text, "UNIT_SETTLER")
    context["$SPY"] = _first_matching(units_text, "UNIT_SPY")
    context["$DAMAGED"] = _first_matching(units_text, "[HP:")
    context["$GREAT_PERSON"] = _first_matching(units_text, "UNIT_GREAT_")
    context["$RELIGIOUS_UNIT"] = _first_matching(
        units_text, "UNIT_MISSIONARY"
    ) or _first_matching(units_text, "UNIT_APOSTLE")
    context["$IDLE_TRADER"] = _idle_trader(units_text)

    # `improve` acts where the builder stands, so the unit and the improvement
    # have to come from the same `Can build` line.
    improve_unit, improvement = _builder_with_buildable(units_text)
    context["$IMPROVE_UNIT"] = improve_unit
    context["$IMPROVEMENT"] = improvement

    builders = _all_matching(units_text, "UNIT_BUILDER")
    context["$BUILDER"] = builders[0] if builders else None
    context["$SLEEPER"] = builders[1] if len(builders) > 1 else None

    # The numbered slots feed fortify / alert / automate, which a civilian
    # cannot do — a Trader in a slot recorded
    # `Error: CANNOT_ALERT|Unit cannot be put on alert`. Take combat units
    # only, and keep off any unit a role placeholder has already claimed, so
    # two verbs never land on one unit.
    claimed = set(builders)
    for key, value in context.items():
        if key in ROLE_PLACEHOLDERS and isinstance(value, int):
            claimed.add(value)

    military = [uid for uid in _military_units(units_text) if uid not in claimed]
    for slot in range(5):
        context[f"$UNIT_{slot}"] = (
            military[slot]
            if slot < len(military)
            else (military[-1] if military else 0)
        )
    if len(military) < 5:
        print(
            f"! Only {len(military)} unclaimed combat units — dispatcher verbs "
            f"will share units and may record interference errors.",
            file=sys.stderr,
        )

    # Destroyed by `delete`, so it must not be a unit any other verb needs.
    # A first cut reserved only the numbered slots and the builders, which
    # handed `delete` the Great Person that `activate` was about to use.
    # Combat units only, for the same reason the slots are: a deleted Settler
    # or Trader is a far more expensive fixture to rebuild.
    reserved = set(claimed) | set(military[:5])
    spare = [uid for uid in _military_units(units_text) if uid not in reserved]
    context["$EXPENDABLE"] = spare[-1] if spare else None

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
    production = await _call(
        client, "get_production_options", {"city_id": context["$CITY"]}
    )
    context["$DISTRICT"] = (
        _first_token(production, "DISTRICT_") or "DISTRICT_ENCAMPMENT"
    )
    context["$WONDER"] = _first_wonder(production) or "BUILDING_PYRAMIDS"
    context["$UNIT_TYPE"] = _first_token(production, "UNIT_") or "UNIT_WARRIOR"

    research = await _call(client, "get_research_options", {})
    context["$TECH"] = _researchable_tech(research) or "TECH_MINING"

    print("Resolved:", {k: v for k, v in context.items() if v is not None})

    resolved = []
    skipped = []
    for tool, arguments, mutates in plan:
        filled = {}
        missing = []
        for key, value in arguments.items():
            if not isinstance(value, str) or not value.startswith("$"):
                filled[key] = value
                continue
            substituted = context.get(value, value)
            if substituted is None:
                missing.append(value)
            else:
                filled[key] = substituted
        if missing:
            skipped.append((tool, arguments, missing))
            continue
        resolved.append((tool, filled, mutates))

    for tool, arguments, missing in skipped:
        verb = arguments.get("action", "")
        label = f"{tool}({verb})" if verb else tool
        print(f"  skip {label}: this save has no {', '.join(missing)}")

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


def _all_matching(text: str, needle: str) -> list[int]:
    """Every unit id whose line mentions `needle`, in narration order."""
    import re

    found = []
    for line in text.splitlines():
        if needle in line.upper():
            match = re.search(r"id[=: ](\d+)", line)
            if match:
                found.append(int(match.group(1)))
    return found


def _military_units(text: str) -> list[int]:
    """Unit ids that can fortify, alert and automate.

    `narrate_units` prints `CS:` only for a unit with combat strength, so it
    separates the military units from Settlers, Traders and Builders exactly
    where the dispatcher verbs need the line drawn.
    """
    import re

    found = []
    for line in text.splitlines():
        if "CS:" not in line:
            continue
        match = re.search(r"id[=: ](\d+)", line)
        if match:
            found.append(int(match.group(1)))
    return found


def _idle_trader(text: str) -> int | None:
    """A trader not already on a route.

    `teleport` and `trade_route` both refuse a trader mid-route, and
    `narrate_units` marks those with `[ON ROUTE: ...]`.
    """
    import re

    for line in text.splitlines():
        if "UNIT_TRADER" not in line.upper():
            continue
        if "ON ROUTE" in line.upper():
            continue
        match = re.search(r"id[=: ](\d+)", line)
        if match:
            return int(match.group(1))
    return None


def _builder_with_buildable(text: str) -> tuple[int | None, str | None]:
    """A builder and something it can build where it currently stands.

    `narrate_units` prints `>> Can build: ...` on the line after the unit it
    belongs to, and only when the game itself reported improvements as legal
    on that tile — which is the precondition `improve` needs.
    """
    import re

    last_unit_id = None
    for line in text.splitlines():
        match = re.search(r"id[=: ](\d+)", line)
        if match:
            last_unit_id = int(match.group(1))
            continue
        if ">> Can build:" in line and last_unit_id is not None:
            improvement = _first_token(line, "IMPROVEMENT_")
            if improvement:
                return last_unit_id, improvement
    return None, None


def _coords(text: str) -> list[tuple[int, int]]:
    """Positions as the narrator prints them: `at (45,12)`."""
    import re

    return [(int(x), int(y)) for x, y in re.findall(r"\((\d+),\s*(\d+)\)", text)]


def _city_positions(cities_text: str) -> list[tuple[int, int]]:
    """City centre tiles only.

    `_coords` over the whole block also matches district tiles —
    `DISTRICT_HOLY_SITE(46,12)` — so taking its second entry aimed `teleport`
    at a district and recorded
    `Error: CANNOT_TELEPORT|Cannot teleport trader to (46,12)`. A city line is
    the one carrying `(pop N) at (x,y)`.
    """
    import re

    found = []
    for line in cities_text.splitlines():
        match = re.search(r"\(pop \d+\) at \((\d+),\s*(\d+)\)", line)
        if match:
            found.append((int(match.group(1)), int(match.group(2))))
    return found


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


def _researchable_tech(research: str) -> str | None:
    """A tech from the Available list that is not the one already running.

    Taking the first `TECH_` token in the whole output recorded
    `Error: ALREADY_COMPLETED|TECH_SAILING is already researched`: the current
    research heads the available list, and by the time the write ran the game
    had finished it. Read the Available section, and skip the entry whose
    display name matches the `Researching:` header.
    """
    import re

    running = re.search(r"^Researching:\s*(.+?)\s*\(", research, flags=re.M)
    running_name = running.group(1).strip() if running else ""

    section = re.split(r"^Available techs:", research, flags=re.M)
    if len(section) < 2:
        return None

    for line in section[1].splitlines():
        if not line.strip():
            continue  # `re.split` leaves the rest of the header line first
        if not line.startswith("  "):
            break  # the Available block has ended
        match = re.match(r"\s*(.+?)\s*\(([A-Z_]+)\)", line)
        if not match:
            continue
        display_name, tech_type = match.group(1), match.group(2)
        if display_name == running_name:
            continue
        if not tech_type.startswith("TECH_"):
            continue
        return tech_type
    return None


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


async def _clear_popups(conn) -> None:
    """Dismiss whatever is on screen, between recorded calls.

    Safe to run on the recording connection: `recording.record` is a no-op
    while no tool call is in flight, so this traffic lands in no recording.
    """
    from civ_mcp.game_lifecycle import dismiss_popup

    try:
        await dismiss_popup(conn)
    except Exception as exc:
        print(f"  ! could not clear popups: {exc}")


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
    # Kept so `_clear_popups` can reach the same connection the tools use.
    live: dict[str, object] = {}

    def _connect():
        conn = GameConnection()
        live["conn"] = conn
        return conn

    with server.testing_overrides(
        connection_factory=_connect, background_services=False
    ):
        async with create_connected_server_and_client_session(
            server.mcp, raise_exceptions=False
        ) as client:
            # The resolver calls real tools (`get_units`, `get_cities`,
            # `get_production_options`, …) to fill in live ids. Those calls must
            # not be recorded.
            resolved = await _resolve(client, plan)

            if dry_run:
                for tool, arguments, _ in resolved:
                    print(f"  would record {tool}({_short(arguments)})")
                print("\nDry run — nothing written.")
                return

            recorder = recording.enable(RECORDING_DIR, scenario)
            discarded = 0
            for tool, arguments, _ in resolved:
                # Clear the UI before recording, outside the recording bracket.
                # Every mutating tool polls for popups first, and when that poll
                # finds one it runs the full dismissal — which probes each Lua
                # state individually, driven by `conn.lua_states`. A replay has
                # no state table to drive that loop, so those probes are
                # recorded and never re-issued, and the call replays out of
                # step. Starting clean keeps each recording to the traffic the
                # tool itself issues. The dismissal path is covered live, in
                # `tests/e2e/`, which is where it can be covered honestly.
                if live.get("conn") is not None:
                    await _clear_popups(live["conn"])

                before = len(recorder.written)
                text = await _call(client, tool, arguments)
                first = text.split("\n", 1)[0][:90]

                # A failed call is not a fixture. `_resolve` skips the verbs it
                # can predict, but some preconditions are not visible in any
                # tool's output — whether a Missionary is next to a city,
                # whether a Great Person stands on its district. Rather than
                # guess at those, keep the call and throw the recording away
                # when the game refuses it.
                if text.startswith("Error"):
                    for path in recorder.written[before:]:
                        path.unlink(missing_ok=True)
                    del recorder.written[before:]
                    discarded += 1
                    print(f" ! {tool}({_short(arguments)}) -> {first}")
                    print("     discarded — the game refused this call")
                    continue

                print(f"   {tool}({_short(arguments)}) -> {first}")

            print(
                f"\nWrote {len(recorder.written)} recordings to {RECORDING_DIR / scenario}"
            )
            if discarded:
                print(
                    f"Discarded {discarded} refused call(s); nothing stale was left behind."
                )


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
