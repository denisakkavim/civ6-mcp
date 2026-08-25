#!/usr/bin/env python3
"""Record game traffic into replayable recordings.

Drives the MCP server through a scripted sequence of tool calls against a
running Civ 6, teeing every Lua round trip into
``tests/data/recordings/<scenario>/``. Those recordings are what let `pytest`
exercise the whole stack offline.

    uv run python scripts/install_saves.py                    # once
    # launch Civ 6 with EnableTuner=1 and load 0T_TURN37_INCA
    uv run python scripts/record_game_traffic.py --scenario turn37

Stage 4 split the four dispatchers into eleven tools with fixed signatures.
The `dispatchers` subset now records those eleven, one entry per verb where a
verb survived. The recordings of `unit_action`, `city_attack`, `spy_action` and
`skip_remaining_units` were deleted with the tools: a recording cannot replay
through a tool that no longer exists.

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
    ("get_deal_options", {"player_id": "$MET_PLAYER"}, False),
    (
        "run_lua",
        {"code": 'print("RECORDED|" .. Game.GetCurrentGameTurn())', "context": "gamecore"},
        False,
    ),
    ("get_unit_promotions", {"unit_id": "$UNIT"}, False),
    (
        "get_pathing_estimate",
        {"unit_id": "$UNIT", "target_x": "$CITY_X", "target_y": "$CITY_Y"},
        False,
    ),
]

# The eleven tools Stage 4 split the dispatchers into. One recording per verb,
# so a change to any of them shows as a diff rather than as a surprise.
#
# Every verb gets its **own unit**. Run against a single unit they interfere:
# `fortify` and `skip` end that unit's turn, so a later `move_unit` records
# `NO_MOVES` and the recording captures a failure instead of the behaviour.
# `move_unit` runs first for the same reason.
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
        "move_unit",
        {"unit_id": "$UNIT_0", "target_x": "$MOVE_X", "target_y": "$MOVE_Y"},
        True,
    ),
    ("unit_stance", {"unit_id": "$UNIT_1", "stance": "fortify"}, True),
    ("unit_stance", {"unit_id": "$UNIT_2", "stance": "skip"}, True),
    ("unit_stance", {"unit_id": "$UNIT_3", "stance": "alert"}, True),
    ("unit_stance", {"unit_id": "$UNIT_4", "stance": "automate"}, True),
    # --- conditional on what the save contains ---------------------------
    # `improve` takes no coordinates here: the builder acts on the tile it is
    # standing on, so the resolver only offers this verb when `get_units`
    # reports a `Can build` line for that builder.
    (
        "builder_work",
        {
            "unit_id": "$IMPROVE_UNIT",
            "work": "improve",
            "improvement_type": "$IMPROVEMENT",
        },
        True,
    ),
    # Sleep is refused for a unit that has already fortified or alerted, which
    # most military units in a mid-game save have — and the builder above is
    # busy improving. So this wants a *second* builder, and skips otherwise.
    ("unit_stance", {"unit_id": "$SLEEPER", "stance": "sleep"}, True),
    ("unit_stance", {"unit_id": "$DAMAGED", "stance": "heal"}, True),
    # `activate_great_person` also needs the Great Person to be standing on its
    # matching district. It spawns on the city centre instead, so LATE_PLAN
    # records the move-then-act form, which is the one that works.
    ("activate_great_person", {"unit_id": "$GREAT_PERSON"}, True),
    ("spread_religion", {"unit_id": "$RELIGIOUS_UNIT"}, True),
    # Transfer first: it needs an idle trader, and establishing a route
    # consumes exactly that idleness.
    (
        "send_unit_to_city",
        {"unit_id": "$IDLE_TRADER", "city_id": "$OTHER_CITY"},
        True,
    ),
    # A different trader: the transfer spends the first one's moves.
    (
        "establish_trade_route",
        {"unit_id": "$SECOND_TRADER", "city_id": "$TRADE_DEST"},
        True,
    ),
    ("send_unit_to_city", {"unit_id": "$SPY", "city_id": "$OTHER_CITY"}, True),
    # A *different* spy: the one above has just left the map, and a mission
    # needs a spy that has already arrived. Skipped unless `get_spies` reports
    # one with an operation other than TRAVEL available where it stands.
    ("spy_mission", {"unit_id": "$PLACED_SPY", "mission": "$SPY_OP"}, True),
    (
        "attack",
        {"attacker_id": "$UNIT_2", "target_x": "$ATTACK_X", "target_y": "$ATTACK_Y"},
        True,
    ),
    # The rest of the builder verbs. Each acts where the unit stands, so each
    # is refused unless the tile suits — which is what the specialist saves
    # were made to provide. A refusal costs nothing: the recorder discards it
    # and prints the reason.
    (
        "builder_work",
        {
            "unit_id": "$BUILDER_2",
            "work": "repair",
            "target_x": "$PILLAGED_X",
            "target_y": "$PILLAGED_Y",
        },
        True,
    ),
    ("builder_work", {"unit_id": "$BUILDER_2", "work": "remove_feature"}, True),
    ("builder_work", {"unit_id": "$BUILDER_2", "work": "remove_improvement"}, True),
    ("builder_work", {"unit_id": "$ENGINEER", "work": "build_route"}, True),
    ("disband_unit", {"unit_id": "$BUILDER_2", "mode": "sacrifice_charges"}, True),
    # Founding reshapes ownership and city lists, so it follows every read-like
    # verb above.
    ("found_city", {"unit_id": "$SETTLER"}, True),
]

WRITE_PLAN: list[tuple[str, dict, bool]] = [
    # City attacks run first, before anything that can open a leader screen.
    # `diplomacy_action` below starts an encounter, and an encounter is a
    # blocking popup: every mutating call issued behind one is swallowed by the
    # InGame context and reports whatever it would have reported anyway. These
    # two were recorded that way once and came back "already fired this turn",
    # which was not what had happened.
    ("attack", {"attacker_id": "$ATTACKING_CITY", "target_x": "$ATTACK_X", "target_y": "$ATTACK_Y"}, True),
    # Only one city strike is recorded. A second would be the Encampment
    # firing after the City Center, which is the interesting half of the
    # behaviour — but `attack` carries no discriminator argument, so both
    # calls slug to `attack.json` and the second overwrites the first. The
    # City Center recording is the one worth keeping: it carries the "spare"
    # line that tells the agent a second strike is loaded. The Encampment
    # firing is covered by a live check instead.
    ("set_city_production", {"city_id": "$CITY", "item_type": "$UNIT_TYPE"}, True),
    ("set_tech", {"tech_type": "$TECH"}, True),
    ("set_civic", {"civic_type": "$CIVIC"}, True),
    ("set_city_focus", {"city_id": "$CITY", "focus": "production"}, True),
    ("set_policies", {"assignments": {"$POLICY_SLOT": "$POLICY"}}, True),
    # These four compete for the same gold, and a mid-game empire cannot
    # afford all of them. Cheapest first, so the most calls land per scenario;
    # a richer save (turn 37 holds 275 gold) then covers the rest.
    # These four compete for one treasury, so the order is by what each one
    # buys rather than by cost.
    #
    # `diplomacy_action` first: a delegation is cheap, and it opens the
    # encounter that `respond_to_diplomacy` in the late plan needs. Starving it
    # costs two tools, not one.
    ("diplomacy_action", {"player_id": "$MET_PLAYER", "action": "DIPLOMATIC_DELEGATION"}, True),
    # Then `purchase_item`, the most expensive and the hardest to place: only
    # turn 57 has ever afforded it. The two below are covered elsewhere in the
    # corpus, so they are the right ones to lose when the gold runs out.
    ("purchase_item", {"city_id": "$CITY", "item_type": "$PURCHASABLE_ITEM"}, True),
    ("upgrade_unit", {"unit_id": "$UPGRADEABLE"}, True),
    ("purchase_tile", {"city_id": "$CITY", "target_x": "$TILE_X", "target_y": "$TILE_Y"}, True),
    ("promote_governor", {"governor_type": "$GOVERNOR", "promotion_type": "$GOVERNOR_PROMOTION"}, True),
    ("assign_governor", {"governor_type": "$GOVERNOR", "city_id": "$OTHER_CITY"}, True),
    # Faith, not gold: the empire usually has faith banked, and `recruit`
    # needs Great Person points that no save holds.
    (
        "great_person_action",
        {"individual_id": "$GP_CANDIDATE", "action": "patronize", "yield_type": "YIELD_FAITH"},
        True,
    ),
    # `test` mode asks the game what it would accept without committing, so a
    # recording of it does not reshape the diplomatic state of the save.
    ("propose_deal", {"player_id": "$MET_PLAYER", "mode": "test", "offer_gold": 50}, True),
    # Each of these needs a game state the Inca saves never reach. The
    # Barbarossa saves were made for them, one condition per save.
    ("propose_peace", {"player_id": "$ENEMY_PLAYER"}, True),
    ("send_envoy", {"player_id": "$ENVOY_TARGET"}, True),
    ("choose_dedication", {"dedication_index": "$DEDICATION_INDEX"}, True),
    ("choose_pantheon", {"belief_type": "$PANTHEON_BELIEF"}, True),
    ("promote_unit", {"unit_id": "$PROMOTABLE", "promotion_type": "$PROMOTION"}, True),
    ("appoint_governor", {"governor_type": "$APPOINTABLE_GOVERNOR", "city_id": "$CITY"}, True),
    # Takes no city id: the game holds exactly one city awaiting a decision.
    # Refused by name when there is none, so it costs a scenario nothing.
    ("resolve_city_capture", {"action": "keep"}, True),
    ("set_government", {"government_type": "$GOVERNMENT"}, True),
    # Only legal while a session is open. Turn 37 is the one save that reports
    # "World Congress: FIRES THIS TURN"; everywhere else this skips.
    (
        "queue_world_congress_votes",
        {"votes": [{"hash": "$WC_HASH", "option": 1, "target": 0, "votes": 1}]},
        True,
    ),
]


# Runs after WRITE_PLAN, against a second resolution pass. Everything here
# acts on a unit that did not exist when the first pass ran — the Great Person
# that `great_person_action(patronize)` creates. A single up-front resolution
# cannot name it.
LATE_PLAN: list[tuple[str, dict, bool]] = [
    ("get_great_person_sites", {"unit_id": "$GREAT_PERSON"}, False),
    # A Great Person activates on its own district, and it spawns on the city
    # centre — so this uses Stage 3.2's move-then-act form rather than acting
    # in place, which was refused with CANNOT_ACTIVATE.
    (
        "activate_great_person",
        {
            "unit_id": "$GREAT_PERSON",
            "target_x": "$GP_DISTRICT_X",
            "target_y": "$GP_DISTRICT_Y",
        },
        True,
    ),
    # These two end other units' turns, so they run after every write that
    # needs a unit with moves left. `skip_remaining_units` sat in the
    # dispatcher plan once and silently starved `upgrade_unit`, which then
    # failed with a message that read like a gold problem: "cost:30g have:58g".
    ("skip_remaining_units", {}, True),
    ("disband_unit", {"unit_id": "$EXPENDABLE", "mode": "delete"}, True),
    # `diplomacy_action` earlier in the run opens an encounter with that
    # player. That is the only way any save reaches this tool, and it also
    # unblocks `end_turn`, which refuses to run while one is pending.
    ("respond_to_diplomacy", {"player_id": "$MET_PLAYER", "response": "POSITIVE"}, True),
    # Last, because it ends the turn everything above was recorded in.
    ("end_turn", {}, True),
]


# Placeholders that name a unit by its role. `delete` must not be handed any of
# them, and they are strict: a save without the unit skips the verb rather than
# aiming it at whatever unit happens to be first.
ROLE_PLACEHOLDERS = frozenset(
    {
        "$SETTLER",
        "$SPY",
        "$PLACED_SPY",
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

    # Stage 3.4d gave enemy units in the threat scan an id of their own, and
    # that section is part of get_units output. Every placeholder below picks a
    # unit to act on, so parse only the part that lists units we own —
    # otherwise `delete` aims at a Georgian warrior.
    units_text = units_text.split("Nearby threats")[0]

    unit_ids = _ids(units_text, r"id[=: ](\d+)")
    city_ids = _ids(cities_text, r"id[=: ](\d+)")
    coords = _city_positions(cities_text)
    unit_coords = _coords(units_text)

    if not unit_ids:
        print("! No units found — is a game actually loaded?", file=sys.stderr)
    context["$UNIT"] = unit_ids[0] if unit_ids else 0
    context["$CITY"] = city_ids[0] if city_ids else 0
    context["$CITY_X"], context["$CITY_Y"] = coords[0] if coords else (0, 0)

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
    idle_traders = _all_matching(units_text, "UNIT_TRADER")
    context["$IDLE_TRADER"] = _idle_trader(units_text)
    # A second trader for the route, because `teleport` spends the first one's
    # moves and a trader with none cannot start a route.
    context["$SECOND_TRADER"] = next(
        (uid for uid in idle_traders if uid != context["$IDLE_TRADER"]), None
    )

    # `improve` acts where the builder stands, so the unit and the improvement
    # have to come from the same `Can build` line.
    improve_unit, improvement = _builder_with_buildable(units_text)
    context["$IMPROVE_UNIT"] = improve_unit
    context["$IMPROVEMENT"] = improvement

    builders = _all_matching(units_text, "UNIT_BUILDER")
    context["$BUILDER"] = builders[0] if builders else None
    context["$ENGINEER"] = _first_matching(units_text, "UNIT_MILITARY_ENGINEER")
    # A second builder for the charge-consuming verbs, so they do not compete
    # with `improve` for the one builder a save may hold.
    context["$BUILDER_2"] = builders[1] if len(builders) > 1 else None
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

    # `upgrade_unit` needs the unit to still have its turn. A first cut let
    # $UPGRADEABLE and $UNIT_3 resolve to the same Slinger, so `alert` spent
    # its turn and the upgrade was then refused with a message that read like
    # a gold problem: "cost:30g have:58g".
    context["$UPGRADEABLE"] = _upgradeable_unit(units_text)
    if isinstance(context["$UPGRADEABLE"], int):
        claimed.add(context["$UPGRADEABLE"])

    # A promotable unit must also keep its turn, for the same reason.
    context["$PROMOTABLE"], context["$PROMOTION"] = await _promotable(client, unit_ids)
    if isinstance(context["$PROMOTABLE"], int):
        claimed.add(context["$PROMOTABLE"])

    # `build_route` needs the engineer to still have its turn, so reserve it
    # too. Unreserved it landed in a numbered slot, a stance verb spent its
    # turn, and build_route was refused with NO_MOVES.
    if isinstance(context.get("$ENGINEER"), int):
        claimed.add(context["$ENGINEER"])

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
    context["$CIVIC"] = _researchable_civic(research)

    overview = await _call(client, "get_game_overview", {})
    gold, faith = _gold(overview), _faith(overview)

    # A building first: buying a unit into a city whose centre tile already
    # holds one is refused with STACKING_CONFLICT, and a city centre almost
    # always holds a garrison. A unit is the fallback, because a poor empire
    # can often afford one when it cannot afford any building — turn 57 buys
    # a Settler for 340g and has no building it can reach.
    context["$PURCHASABLE_ITEM"] = _affordable_building(
        production, gold
    ) or _affordable_unit(production, gold)

    tiles = await _call(client, "get_purchasable_tiles", {"city_id": context["$CITY"]})
    tile = _purchasable_tile(tiles)
    context["$TILE_X"], context["$TILE_Y"] = tile if tile else (None, None)

    diplomacy = await _call(client, "get_diplomacy", {})
    context["$MET_PLAYER"] = _met_major_player(diplomacy)
    context["$ENEMY_PLAYER"] = _enemy_player(diplomacy)
    context["$ENVOY_TARGET"] = _envoy_city_state(
        await _call(client, "get_city_states", {})
    )
    context["$DEDICATION_INDEX"] = _dedication_index(
        await _call(client, "get_dedications", {})
    )
    context["$PANTHEON_BELIEF"] = _pantheon_belief(
        await _call(client, "get_belief_options", {})
    )


    great_people = await _call(client, "get_great_people", {})
    context["$GP_CANDIDATE"] = _affordable_great_person(great_people, faith)

    policies = await _call(client, "get_policies", {})
    context["$GOVERNMENT"] = _other_government(policies)

    # Only resolvable once a Great Person exists, so this reads as None on the
    # first pass and fills in on the second.
    great_person = context.get("$GREAT_PERSON")
    if isinstance(great_person, int):
        sites = await _call(client, "get_great_person_sites", {"unit_id": great_person})
        district = _activation_district(sites)
    else:
        district = None
    context["$GP_DISTRICT_X"], context["$GP_DISTRICT_Y"] = (
        district if district else (None, None)
    )

    congress = await _call(client, "get_world_congress", {})
    context["$WC_HASH"] = _congress_resolution_hash(congress)
    policy = _slottable_policy(policies)
    context["$POLICY_SLOT"], context["$POLICY"] = policy if policy else (None, None)

    governors = await _call(client, "get_governors", {})
    context["$APPOINTABLE_GOVERNOR"] = _appointable_governor(governors)
    governor = _governor_with_promotion(governors)
    context["$GOVERNOR"], context["$GOVERNOR_PROMOTION"] = (
        governor if governor else (None, None)
    )

    # Repair acts on the tile the builder stands on, so it needs somewhere
    # pillaged to go. Stage 3.2's move-then-act carries it there.
    pillaged = _pillaged_tile(cities_text)
    context["$PILLAGED_X"], context["$PILLAGED_Y"] = pillaged if pillaged else (None, None)

    attack = _attack_targets(cities_text)
    if attack:
        city_id, targets = attack
        context["$ATTACKING_CITY"] = city_id
        context["$ATTACK_X"], context["$ATTACK_Y"] = targets[0]
        if len(targets) > 1:
            context["$ATTACK_X2"], context["$ATTACK_Y2"] = targets[1]
        else:
            context["$ATTACK_X2"], context["$ATTACK_Y2"] = None, None
    else:
        context["$ATTACKING_CITY"] = None
        context["$ATTACK_X"], context["$ATTACK_Y"] = None, None
        context["$ATTACK_X2"], context["$ATTACK_Y2"] = None, None

    # A second city: assign_governor moves a governor somewhere new, and
    # send_unit_to_city needs a destination that is not where the unit is.
    # None when the empire has only one, which skips those verbs.
    context["$OTHER_CITY"] = city_ids[1] if len(city_ids) > 1 else None

    spies_text = await _call(client, "get_spies", {})
    context["$PLACED_SPY"], context["$SPY_OP"] = _placed_spy(
        spies_text, context["$SPY"]
    )

    if context["$IDLE_TRADER"] is not None:
        destinations = await _call(
            client, "get_trade_destinations", {"unit_id": context["$IDLE_TRADER"]}
        )
        context["$TRADE_DEST"] = _trade_destination(destinations)
    else:
        context["$TRADE_DEST"] = None

    print("Resolved:", {k: v for k, v in context.items() if v is not None})

    resolved = []
    skipped = []
    for tool, arguments, mutates in plan:
        filled = {}
        missing = []
        for key, value in arguments.items():
            substituted = _substitute(value, context, missing)
            if substituted is None:
                continue
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


def _other_government(policies: str) -> str | None:
    """A government we could switch to, that is not the one we hold.

    No tool lists the available governments, so this picks from the classical
    tier, which Political Philosophy unlocks as a set. A save in a later era
    holds one of these anyway, so the swap is legal; a save that somehow holds
    none of them resolves to None and the entry is skipped rather than
    recording a refusal.
    """
    import re

    current = re.search(r"\((GOVERNMENT_[A-Z_]+)\)", policies)
    current_type = current.group(1) if current else ""
    for candidate in (
        "GOVERNMENT_OLIGARCHY",
        "GOVERNMENT_CLASSICAL_REPUBLIC",
        "GOVERNMENT_AUTOCRACY",
    ):
        if candidate != current_type:
            return candidate
    return None


def _affordable_building(production: str, gold: int) -> str | None:
    """A building the city can buy outright with the gold we hold.

    Buildings, not units: a unit purchased into a city centre that already
    holds one is refused with STACKING_CONFLICT, and a city centre almost
    always holds a garrison.
    """
    import re

    for match in re.finditer(
        r"(BUILDING_[A-Z_]+)\s*\(cost \d+, \d+ turns, buy: (\d+)g\)", production
    ):
        if int(match.group(2)) <= gold:
            return match.group(1)
    return None


def _congress_resolution_hash(congress: str) -> int | None:
    """A resolution hash to vote on, when a session is actually open.

    The hash reaches the agent only while the congress is in session; between
    sessions the read lists upcoming policies without one. So this resolves on
    the saves that catch a session and skips everywhere else.
    """
    import re

    match = re.search(r"hash:\s*(-?\d+)", congress)
    return int(match.group(1)) if match else None


def _activation_district(sites: str) -> tuple[int, int] | None:
    """The tile of a district this Great Person can actually activate on.

    `get_great_person_sites` marks each city CAN ACTIVATE or "needs move", and
    prints the district's own coordinates rather than the city centre — which
    is where the unit spawns, and why activating in place is refused.
    """
    import re

    for line in sites.splitlines():
        if "CAN ACTIVATE" not in line:
            continue
        match = re.search(r"\((\d+),(\d+)\)", line)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def _enemy_player(diplomacy: str) -> int | None:
    """A major civ we are at war with."""
    import re

    for line in diplomacy.splitlines():
        if "WAR" not in line.upper():
            continue
        match = re.search(r"\[player (\d+)\]", line)
        if match:
            return int(match.group(1))
    return None


def _envoy_city_state(city_states: str) -> int | None:
    """A city-state we can send an envoy to right now.

    `get_city_states` marks these `[can send]`, and only does so when tokens
    are actually available — so this resolves to None between envoy grants.
    """
    import re

    for line in city_states.splitlines():
        if "[can send]" not in line:
            continue
        match = re.search(r"\[player (\d+)\]", line)
        if match:
            return int(match.group(1))
    return None


def _dedication_index(dedications: str) -> int | None:
    """The index of a dedication offered this era, if one is being offered."""
    import re

    match = re.search(r"^\s*(\d+)[.):]\s", dedications, flags=re.M)
    return int(match.group(1)) if match else None


def _pantheon_belief(beliefs: str) -> str | None:
    """A pantheon belief to choose, when no pantheon has been chosen yet."""
    import re

    if "Pantheon:" in beliefs and "index" in beliefs:
        return None  # already has one
    match = re.search(r"\((BELIEF_[A-Z_]+)\)", beliefs)
    return match.group(1) if match else None


async def _promotable(client, unit_ids: list[int]) -> tuple[int | None, str | None]:
    """(unit, promotion) for the first unit that can actually promote.

    Asks `get_unit_promotions` rather than reading `get_units`. The
    NEEDS PROMOTION marker there is hardcoded off — `lua/units.py` sets
    `promo = "0"` on purpose, because an XP-based check fires a turn early and
    double-promotes. So the marker can never appear, and a resolver that reads
    it can never find a promotable unit, however many the save holds.
    """
    import re

    for unit_id in unit_ids[:8]:
        text = await _call(client, "get_unit_promotions", {"unit_id": unit_id})
        if not text.startswith("Promotions for"):
            continue
        match = re.search(r"\((PROMOTION_[A-Z_]+)\)", text)
        if match:
            return unit_id, match.group(1)
    return None, None


def _appointable_governor(governors: str) -> str | None:
    """A governor we could appoint, when a point is available."""
    import re

    points = re.search(r"Governor Points:\s*(\d+) available", governors)
    if not points or int(points.group(1)) == 0:
        return None
    section = governors.split("Available to appoint")
    if len(section) < 2:
        return None
    match = re.search(r"\((GOVERNOR_[A-Z_]+)\)", section[1])
    return match.group(1) if match else None


def _pillaged_tile(cities: str) -> tuple[int, int] | None:
    """A pillaged tile a builder could be sent to repair."""
    import re

    match = re.search(r"PILLAGED TILES:[^\n]*?@(\d+),(\d+)", cities)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _substitute(value, context: dict, missing: list):
    """Replace $PLACEHOLDERs, descending into the containers a tool may take.

    `set_policies` takes a {slot: policy} mapping and
    `queue_world_congress_votes` a list of vote dicts, so a placeholder can sit
    at any depth rather than only at the top level.
    """
    if isinstance(value, dict):
        return {
            _substitute(k, context, missing): _substitute(v, context, missing)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_substitute(item, context, missing) for item in value]
    if not isinstance(value, str) or not value.startswith("$"):
        return value
    resolved = context.get(value, value)
    if resolved is None:
        missing.append(value)
        return None
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


def _placed_spy(text: str, exclude: int | None) -> tuple[int | None, str | None]:
    """A spy whose current city already allows a mission, and that mission.

    `spy_mission` takes no coordinates: it acts where the spy stands, so the
    only spy worth recording is one that has already arrived somewhere with an
    operation available. `narrate_spies` prints those as `ops: A, B`.

    *exclude* is the spy `send_unit_to_city` will send earlier in the same run.
    Handing the same spy to both takes it off the map first, and the mission is
    then refused with SPY_IN_TRANSIT — which is the guard working, but it
    records nothing. A spy with no TRAVEL among its operations is preferred:
    that is a spy sitting inside a foreign city, which is the case a mission
    fixture should hold.
    """
    import re

    best = (None, None)
    for line in text.splitlines():
        match = re.search(r"id:(\d+)\b.*\bops:\s*(.+)$", line)
        if not match:
            continue
        spy_id = int(match.group(1))
        if spy_id == exclude:
            continue
        names = [name.strip() for name in match.group(2).split(",")]
        missions = [name for name in names if name and name != "TRAVEL"]
        if not missions:
            continue
        if "TRAVEL" not in names:
            return spy_id, missions[0]
        if best == (None, None):
            best = (spy_id, missions[0])
    return best


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


def _researchable_civic(research: str) -> str | None:
    """A civic from the Available list that is not the one already running.

    Same shape and same trap as `_researchable_tech`: the current civic heads
    the available list, and setting it records ALREADY_COMPLETED.
    """
    import re

    running = re.search(r"^Civic:\s*(.+?)\s*\(", research, flags=re.M)
    running_name = running.group(1).strip() if running else ""

    section = re.split(r"^Available civics:", research, flags=re.M)
    if len(section) < 2:
        return None
    for line in section[1].splitlines():
        if not line.strip():
            continue
        if not line.startswith("  "):
            break
        match = re.match(r"\s*(.+?)\s*\(([A-Z_]+)\)", line)
        if not match:
            continue
        display_name, civic_type = match.group(1), match.group(2)
        if display_name == running_name or not civic_type.startswith("CIVIC_"):
            continue
        return civic_type
    return None


def _slottable_policy(policies: str) -> tuple[int, str] | None:
    """(slot index, policy type) for a policy the government can actually hold.

    Takes the slot from the existing layout rather than assuming slot 0, and
    the policy from the Available list. A wildcard slot accepts anything, so
    prefer one when the output offers it.
    """
    import re

    slots = re.findall(r"^  Slot (\d+) \((\w+)\):", policies, flags=re.M)
    if not slots:
        return None
    wildcard = [int(i) for i, kind in slots if kind.lower() == "wildcard"]
    slot = wildcard[0] if wildcard else int(slots[0][0])

    section = re.split(r"^Available policies:", policies, flags=re.M)
    if len(section) < 2:
        return None
    match = re.search(r"\((POLICY_[A-Z_]+)\)", section[1])
    return (slot, match.group(1)) if match else None


def _purchasable_tile(tiles: str) -> tuple[int, int] | None:
    """The first tile the city can buy, as (x, y)."""
    import re

    match = re.search(r"^\s*\((\d+),(\d+)\):\s*\d+g", tiles, flags=re.M)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _met_major_player(diplomacy: str) -> int | None:
    """A major civ we have met, by player id."""
    import re

    for line in diplomacy.splitlines():
        if "not met" in line:
            continue
        match = re.search(r"\[player (\d+)\]", line)
        if match:
            return int(match.group(1))
    return None


def _affordable_great_person(great_people: str, faith: int) -> int | None:
    """An individual we can patronize with faith we already hold.

    Faith rather than gold: a mid-game empire usually has enough faith banked
    and rarely enough gold, and `recruit` needs Great Person points that no
    save has.
    """
    import re

    blocks = re.split(r"\n(?=  \w)", great_people)
    for block in blocks:
        cost = re.search(r"Patronize:\s*\d+g\s*/\s*(\d+)f", block)
        individual = re.search(r"individual_id:\s*(\d+)", block)
        if cost and individual and int(cost.group(1)) <= faith:
            return int(individual.group(1))
    return None


def _faith(overview: str) -> int:
    import re

    match = re.search(r"Faith:\s*(\d+)", overview)
    return int(match.group(1)) if match else 0


def _governor_with_promotion(governors: str) -> tuple[str, str] | None:
    """(governor type, promotion type) for an appointed governor that can promote."""
    import re

    current = None
    for line in governors.splitlines():
        appointed = re.search(r"\((GOVERNOR_[A-Z_]+)\)", line)
        if appointed and "—" in line:
            current = appointed.group(1)
            continue
        promotion = re.search(r"\((GOVERNOR_PROMOTION_[A-Z_]+)\)", line)
        if promotion and current:
            return current, promotion.group(1)
    return None


def _upgradeable_unit(units: str) -> int | None:
    """A unit the game says can upgrade right now."""
    import re

    for line in units.splitlines():
        if "CAN UPGRADE" not in line:
            continue
        match = re.search(r"id:(\d+)", line)
        if match:
            return int(match.group(1))
    return None


def _attack_targets(cities: str) -> tuple[int, list[tuple[int, int]]] | None:
    """(attacking city id, its target tiles) from that city's CAN ATTACK lines.

    The city id comes from the city block the lines sit under, so the attack is
    issued by a city that can actually reach them. Two tiles are wanted, not
    one: a city fires from each defended district once per turn, so recording
    the second shot needs a second tile — and the two are usually different
    tiles, because the Encampment shoots from its own square.
    """
    import re

    city_id = None
    for line in cities.splitlines():
        city = re.search(r"\[id:(\d+)\]", line)
        if city:
            city_id = int(city.group(1))
            targets: list[tuple[int, int]] = []
            continue
        target = re.search(r"CAN ATTACK:\s*\S+@(\d+),(\d+)", line)
        if target and city_id is not None:
            targets.append((int(target.group(1)), int(target.group(2))))
            if len(targets) >= 2:
                return city_id, targets
    if city_id is not None and targets:
        return city_id, targets
    return None


def _trade_destination(destinations: str) -> int | None:
    """A destination city id a trader can start a route to."""
    import re

    match = re.search(r"\[city:(\d+)\]", destinations)
    return int(match.group(1)) if match else None


def _affordable_unit(production: str, gold: int) -> str | None:
    """A unit the city can buy outright with the gold we hold."""
    import re

    for match in re.finditer(r"(UNIT_[A-Z_]+)\s*\(cost \d+, \d+ turns, buy: (\d+)g\)", production):
        if int(match.group(2)) <= gold:
            return match.group(1)
    return None


def _gold(overview: str) -> int:
    import re

    match = re.search(r"Gold:\s*(\d+)", overview)
    return int(match.group(1)) if match else 0


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


async def _clear_diplomacy(conn) -> None:
    """Close any open leader encounter, between recorded calls.

    `dismiss_popup` cannot reach these. A leader screen lives in
    `ExclusivePopupManager`, and the shallow dismissal the mutating path runs
    walks straight past it — so every command recorded behind one is swallowed
    by the InGame context while the tool still prints whatever it would have
    printed. A run once recorded two city attacks as "already fired this turn"
    with Alexander on screen; neither had fired, and nothing in the output said
    so.

    Runs on the raw connection like `_clear_popups`, so the traffic lands in no
    recording.
    """
    from civ_mcp import lua as lq

    try:
        for _ in range(6):
            lines = await conn.execute_write(lq.build_diplomacy_session_query())
            sessions = lq.parse_diplomacy_sessions(lines)
            if not sessions:
                return
            for session in sessions:
                await conn.execute_write(
                    lq.build_diplomacy_respond(session.other_player_id, "POSITIVE")
                )
        print("  ! a leader encounter would not close — later calls are suspect")
    except Exception as exc:
        print(f"  ! could not clear diplomacy: {exc}")


async def _leave_screen_clear(live) -> None:
    """Close anything the run left on screen, once it is over.

    The last recorded call is `end_turn`, and the turn it starts is where the
    AI opens its encounters — so a run reliably ends with a leader on screen
    and nothing after it to dismiss them. That is not this run's problem; it is
    the *next* one's, and the next thing to touch the game is usually a person
    wondering why nothing works.
    """
    if live.get("conn") is None:
        return
    await _clear_popups(live["conn"])
    await _clear_diplomacy(live["conn"])


async def _run_plan(client, live, recorder, resolved) -> int:
    """Record each call in `resolved`. Returns how many the game refused."""
    discarded = 0
    for tool, arguments, _ in resolved:
        # Clear the UI before recording, outside the recording bracket. Every
        # mutating tool polls for popups first, and when that poll finds one it
        # runs the full dismissal — which probes each Lua state individually,
        # driven by `conn.lua_states`. A replay has no state table to drive
        # that loop, so those probes are recorded and never re-issued, and the
        # call replays out of step. Starting clean keeps each recording to the
        # traffic the tool itself issues. The dismissal path is covered live,
        # in `tests/e2e/`, which is where it can be covered honestly.
        if live.get("conn") is not None:
            await _clear_popups(live["conn"])
            await _clear_diplomacy(live["conn"])

        before = len(recorder.written)
        text = await _call(client, tool, arguments)
        first = text.split("\n", 1)[0][:90]

        # A failed call is not a fixture. `_resolve` skips the verbs it can
        # predict, but some preconditions are not visible in any tool's output
        # — whether a Missionary is next to a city, whether a Great Person
        # stands on its district. Rather than guess at those, keep the call and
        # throw the recording away when the game refuses it.
        if text.startswith("Error"):
            for path in recorder.written[before:]:
                path.unlink(missing_ok=True)
            del recorder.written[before:]
            discarded += 1
            print(f" ! {tool}({_short(arguments)}) -> {first}")
            print("     discarded — the game refused this call")
            continue

        print(f"   {tool}({_short(arguments)}) -> {first}")

    return discarded


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
            discarded = await _run_plan(client, live, recorder, resolved)

            # A second pass, for tools that act on something the first pass
            # created. `great_person_action(patronize)` spawns a Great Person,
            # and nothing can name that unit until it exists — so resolve
            # again, now that it does.
            if plan is not READ_PLAN:
                # Not recorded. `_resolve` calls real tools to find live ids,
                # and recording is already on by now — so without this every
                # probe overwrites the deliberate recording of that same tool
                # with whatever state the game is in part-way through the run.
                # The first pass gets this for free by resolving before
                # `enable`; this one has to ask.
                with recording.paused():
                    late = await _resolve(client, LATE_PLAN)
                if late:
                    print("\n-- second pass --")
                    discarded += await _run_plan(client, live, recorder, late)
            await _leave_screen_clear(live)
            written = {path.name for path in recorder.written}
            stale = sorted(
                path.name
                for path in (RECORDING_DIR / scenario).glob("*.json")
                if path.name not in written
            )
            if stale:
                print(
                    f"\n! {len(stale)} recording(s) this run did not write: "
                    f"{', '.join(stale)}"
                )
                print(
                    "  They are from an earlier plan. Either the call was "
                    "refused this time, or the plan no longer makes it. A "
                    "recording nothing can regenerate cannot be refreshed "
                    "when it breaks."
                )

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
        help="Which calls to record. 'dispatchers' covers the eleven unit tools.",
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
