# Test save wish list

This file says which saves the project still needs, and why. The goal is a
recording for every tool on the surface that Stage 4 leaves behind.

Read `README.md` first. It says what the four saves hold today.

Coverage now: **48 of 70 tools have a recording.**

Three tools will never have one — see below. That leaves 67 tools to reach
today, and about 74 after Stage 4 splits the dispatchers.

## Read this before you make a save

A tool has no recording for one of two reasons. The fix differs, and only the
second reason needs you.

| Reason | Who fixes it |
|---|---|
| `scripts/record_game_traffic.py` never calls the tool | Me. I add the call to a plan and record it against a save we already have |
| No save meets the tool's precondition | You. Play to that state and save |

21 tools have no recording. They split three ways:

- **6 are mine, and still open.** `get_great_person_sites` needs the Great
  Person that `great_person_action(patronize)` now creates, so it wants a
  second resolution pass. `purchase_item`, `purchase_tile` and `upgrade_unit`
  compete for gold that a mid-game empire does not have; a richer save covers
  them, and turn 37 already covers some. `queue_world_congress_votes` needs a
  session that is actually open. `set_government` needs an alternative
  government unlocked.
- **13 need a save.** They are the lists below.
- **3 never get one.** `load_game` and `restart_game` kill and relaunch the
  game. `get_saves` scans the save directory, so its output changes every time
  the game writes an autosave — a fixture that fails for reasons unrelated to
  the code is worse than no fixture.

**Three claims in an earlier version of this file were wrong, and recording
against a live game is what disproved them.** They are corrected below:

- `promote_governor` needs a spare **governor point**, not just an appointed
  governor with promotions listed. `CANNOT_PROMOTE|No governor points
  available` in all four scenarios.
- `attack` and `city_attack` need a **war**. Turn 73 holds an enemy scout in
  range, but `NOT_AT_WAR|Cannot attack UNIT_SCOUT — you are at peace`. Range
  matters too: the same scout was `OUT_OF_RANGE|Target is 3 tiles away (city
  attack range is 2)`.
- `spread_religion` needs the Missionary **in or adjacent to a city**. Turn 73
  has one, at (46,7), two tiles from the nearest city.

Four `unit_action` verbs are also missing and are also mine: `attack`,
`trade_route`, `activate` and `spread_religion`. Turn 73 holds an enemy in
range, an idle trader, and a Missionary that can reach a city in one move.
Stage 4 turns each into a tool of its own, so recording them now gives the
split a before-picture.

Two of those deserve a note, because earlier versions of this file asked for
saves that turned out to be unnecessary:

- **A claimable Great Person.** Turn 73 holds 349 faith, and Marcus Licinius
  Crassus costs 290 faith to patronize. So `great_person_action(patronize)`
  works today, and the unit it creates then unblocks `get_great_person_sites`
  and `activate_great_person`. Only `recruit` still needs a save, because that
  verb needs Great Person points rather than faith.
- **An appointed governor with a promotion.** Turn 73 has two, each with five
  promotions available. `promote_governor` and `assign_governor` need no save.

## The saves I need

Three saves close every remaining gap. Each one is a checklist. Check the
conditions in the game before you save, because several of them are cleared by
ending a turn.

### Save 1 — a war in progress

The largest unlock. Aim for turn 140 or later, at war with a major civ.

| Condition | It unblocks |
|---|---|
| A captured or disloyal city awaiting your decision | `resolve_city_capture`. Stage 2 could only prove that this fails by name |
| A pillaged improvement | `builder_work(work="repair")` |
| A pillaged district | The `type@x,y` output that Stage 3.4d added and nothing has run |
| A damaged unit | `unit_stance(stance="heal")` |
| A unit with a promotion available | `promote_unit`, and the eligibility marker in Stage 4.9 |
| Past the peace cooldown | `propose_peace` |
| An Encampment district of your own | Stage 4.1 asks whether an encampment can attack. Nothing can answer it today |
| An enemy unit within 2 tiles of one of your cities, while at war | `attack` and `city_attack`, and so the general `attack` tool that Stage 4 merges them into. Both are refused at peace, and the city range is 2 |
| A Missionary standing in or next to a city | `spread_religion` |
| A spare governor point | `promote_governor`, and `appoint_governor(city_id=…)` which appoints and assigns in one call |
| An enemy city you can capture on the next turn | Stage 3.4a. A city id encodes its owner, so capturing changes it. No save has ever shown that happen |

A war produces most of this by itself. The last row is the one to plan for:
leave an enemy city at low health rather than taking it, so the capture happens
inside the recording.

### Save 2 — specialist units

These are units that no save has ever held. Any era after the Renaissance will
do. One save can hold all of them at once.

| Condition | It unblocks |
|---|---|
| A Spy, idle, not in transit | `spy_mission` and its 9 missions, and `send_unit_to_city` for a spy. `spy_action` is the only tool that has been in the recording plan since the start and has never once been recorded |
| A Military Engineer with charges | `builder_work(work="build_route")` |
| A Builder standing on a removable feature | `builder_work(work="remove_feature")` |
| A Builder standing on an intact improvement | `builder_work(work="remove_improvement")` |
| The Royal Society policy card slotted, and a Builder on a district under construction | `disband_unit(mode="sacrifice_charges")`. Stage 4.5 asks whether this verb belongs in `disband_unit` at all, and the answer decides where it goes |
| An Aerodrome and an Airport | §6b asks whether airlift is exposed anywhere. If it is not, that is a missing capability rather than a naming problem |

### Save 3 — early game, before a pantheon

Small and quick. Turn 20 to 30 is enough. Stop before you choose a pantheon.

| Condition | It unblocks |
|---|---|
| No pantheon chosen, and enough faith to choose one | `choose_pantheon`, and the pantheon branch of `get_belief_options` |
| A Great Prophet, or the faith to patronize one | `found_religion`, and the founding branch of `get_belief_options` |

Every current save has a religion already, so both branches of
`get_belief_options` have only ever been seen in one state.

## States that a turn boundary destroys

These six are transient. They cannot be reached by playing to a turn number,
because ending a turn clears them. Save at the moment you see them, in whichever
game you are already playing. Any of the three saves above can carry one.

| Condition | It unblocks |
|---|---|
| Envoy tokens available | `send_envoy` |
| An incoming diplomacy session from an AI | `respond_to_diplomacy` |
| An era about to turn, with a dedication to choose | `choose_dedication` |
| A declared friendship, so an alliance is legal | `form_alliance` |
| Enough Great Person points to recruit | `great_person_action(action="recruit")` |

## How to hand a save over

Follow "How to add a save" in `README.md`. In short:

1. Use a save this repository's own server made, on the machine the tests run
   on. A curated save may refuse to load. Section 7 of
   `tool-surface-test-plan.md` records how that was found out.
2. Rename it so that it does not match `0_MCP_*`. The server deletes those
   before it loads a scenario, and the crash-recovery path restores from them.
3. Copy it into this folder and add a row to the table in `README.md`. State
   the civ, the turn, and the number of cities and units.
4. Add the scenario to `SCENARIO_SAVES` in
   `tests/integration/test_recorded_tool_calls.py`.
5. Tell me which of the conditions above the save meets. I will add the plan
   entries and record it.

Keep the Inca saves. The civ trait and the leader trait grant improvements by
different rules, and Inca is the one civ that tests both.

## What will still be untested

Two things, and neither is a save problem.

**Whether the surface is easier for an agent to use.** A recording proves that
a tool still says what it said. It cannot prove that an agent picks the right
tool, or plays a better game. That needs an agent, a full game, and a
comparison. The eval harness was the instrument, and Stage 0a deleted it.

**Whether two tools agree with each other.** Every recording agrees with
itself, because the same code wrote it. During Stage 3 four tools printed raw
game ids while every other tool printed composite ids, and the whole corpus
still replayed clean. Only reading the source caught it.
