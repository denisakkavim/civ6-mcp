# Test save wish list

This file lists the game states that the test saves do not hold. Each state is
a precondition. A tool or a verb stays untested until a save meets it.

Read `README.md` first. It says what the four saves hold today.

Counts at the time of writing:

- 38 of 69 tools have a recording.
- 10 of 20 `unit_action` verbs have a recording.

## A new save is not always the fix

A tool has no recording for one of two reasons. Find the reason before you make
a save.

| Reason | The fix |
|---|---|
| `scripts/record_game_traffic.py` never calls the tool | Add the call to `READ_PLAN`, `DISPATCHER_PLAN`, or `WRITE_PLAN` |
| The plan calls the tool, and no save meets its precondition | Make a save |

Today 30 tools are absent from the plans. Only `spy_action` is in a plan and
still has no recording. A new save alone therefore closes few gaps. Add the
plan entry as well.

Seven verbs need both: `attack`, `trade_route`, `repair`, `remove_feature`,
`remove_improvement`, `build_route`, and `sacrifice_charges`.

## Priority 1: states that no save holds

Each row blocks a verb or a tool that the plan already reaches, or that
Stage 2 could not verify.

| Game state | It unblocks |
|---|---|
| A damaged unit | `unit_action(heal)` |
| A Great Person on its matching district | `unit_action(activate)`, `get_great_person_sites`, `activate_great_person` |
| A Missionary or an Apostle in or next to a city | `unit_action(spread_religion)`, `spread_religion` |
| A pillaged improvement | `unit_action(repair)` |
| A pillaged district | The `type@x,y` output that Stage 3.4d adds |
| A builder on a removable feature | `unit_action(remove_feature)` |
| A builder on an intact improvement | `unit_action(remove_improvement)` |
| A Military Engineer | `unit_action(build_route)` |
| A trader that is not on a route | `unit_action(trade_route)`, `establish_trade_route` |
| A Spy | `spy_action(travel)`, the 9 missions, `spy_mission` |
| The Royal Society card, and a builder on a district tile | `unit_action(sacrifice_charges)`, `disband_unit` |
| A spare governor point | `appoint_governor(city_id=...)`, which appoints and assigns |
| A claimable Great Person | `great_person_action` in all three verbs |
| A captured or a disloyal city awaiting a decision | `resolve_city_capture` |
| A unit with a promotion available | `promote_unit`, and the marker in Stage 4.9 |
| A unit that can upgrade, and the gold to pay | `upgrade_unit`, and the gold cost in Stage 4.9 |

The last three rows matter most. Stage 2 left `great_person_action`,
`resolve_city_capture`, and the governor chain with no live test. Each one
fails cleanly and by name today. That proves the dispatch. It does not prove
the happy path.

## Priority 2: states that Stage 3 and Stage 4 need

| Game state | It unblocks |
|---|---|
| A city with a free district slot, and an available wonder | Stage 3.1. The advisor picks the tile when the agent gives no coordinates |
| A builder whose task is out of reach this turn | Stage 3.2. The `MOVED_PARTIAL` result |
| A unit that arrives with no movement left | Stage 3.2. The `ARRIVED_WAITING` result. This case may not exist. A save is how to find out |
| A city that you can capture this turn | Stage 3.4a. The composite id changes when the owner changes |
| An Encampment district | Stage 4.1. Check whether an encampment can attack |
| An Aerodrome and an Airport | The airlift capability that §6b asks about |

## Do not ask for these again

The saves already hold these states. Check here before you add a row above.

| Game state | Where |
|---|---|
| A founded religion, so `religion_type` holds a value | Turn 63 (Shinto) |
| Foreign cities, for the composite city id | Turn 63 (Georgia) |
| City-states with envoys | Turn 73 (Wolin, La Venta) |
| Enemy units in the threat scan | Turn 73 (2 threats) |
| An enemy unit in range of a city | Turn 73 (`CAN ATTACK: UNIT_SCOUT@43,5`) |
| An idle trader, and a builder on a buildable tile | Turn 73 |
| A civ unique improvement, and a leader unique improvement | Every save. Inca gives Terrace Farm and Qhapaq Ñan |

The Inca row is useful. The civ trait and the leader trait grant improvements
by different rules. One civ tests both. Any save that replaces the Inca saves
loses that.

Turn 73 already holds an enemy unit in range. The `attack` verb therefore
needs a plan entry, not a save.

## The save that would close the most gaps

One save cannot hold every state. The recorder skips a verb when the save
cannot meet its precondition, and it prints the reason. The corpus collects
the verbs across the scenarios. Add saves; do not replace them.

That said, one late-game save closes most of Priority 1 at once. Aim for these
conditions together:

- Turn 120 or later, in the Medieval era or after.
- At war, with a captured city awaiting a decision.
- Pillaged improvements and a pillaged district, which a war produces.
- A Spy, a Missionary, a Military Engineer, and a Great Person on its district.
- A spare governor point, and a claimable Great Person.
- A damaged unit, a unit with a promotion waiting, and an upgradeable unit.
- Enough gold to pay for the upgrade.

Play the game to that state with this repository's own server. Do not curate
the save. `README.md` says why a curated save may not load.

## Two saves for agent versus agent

The intended consumer is a hot-seat harness. Two agents play each other. That
needs a start position with two human players and a documented, balanced start.
No save in this folder gives one. The CivBench saves did, and they need about
15 DLC packs. `README.md` says how to recover them.

Record the civ, the leader, the map, and the difficulty for any such save. A
result from an agent-versus-agent run means nothing without them.

## How to add a save

Follow "How to add a save" in `README.md`. Then delete the row you closed from
this file.
