# Test save wish list

This file says which saves the project still needs, and why. The goal is a
recording for every tool on the surface that Stage 4 leaves behind.

Read `README.md` first. It says what the four saves hold today.

Coverage: **58 of 70 tools, and 16 of 20 `unit_action` verbs**, across 14 scenarios.

The ten Frederick Barbarossa saves closed most of what the Inca saves could
not reach. They gained seven tools — `resolve_city_capture`, `appoint_governor`,
`propose_peace`, `send_envoy`, `choose_pantheon`, `queue_world_congress_votes`
and `respond_to_diplomacy` — and five verbs: `activate`, `attack`, `heal`,
`repair` and `remove_improvement`.

## Read this before you make a save

A tool has no recording for one of two reasons. The fix differs, and only the
second reason needs you.

| Reason | Who fixes it |
|---|---|
| `scripts/record_game_traffic.py` never calls the tool | Me. I add the call to a plan and record it against a save we already have |
| No save meets the tool's precondition | You. Play to that state and save |

12 tools have no recording. They split three ways:

- **3 never get one.** `load_game` and `restart_game` kill and relaunch the
  game; the destructive live test covers them instead. `get_saves` scans the
  save directory, so its output changes every time the game writes an
  autosave — a fixture that fails for reasons unrelated to the code is worse
  than no fixture.
- **1 is mine.** `propose_deal` *is* recorded, but under the label `test_deal`,
  because `_logged` renames it in test mode. Recording it under its own name
  means `mode="send"`, which commits a real deal and reshapes the save.
- **8 need a save.** They are the lists below.

Nine `unit_action` verbs are also missing: `activate`, `attack`, `build_route`,
`heal`, `remove_feature`, `remove_improvement`, `repair`, `sacrifice_charges`
and `spread_religion`. Every one needs a save. Stage 4 turns each into a tool
of its own, so a recording made now also gives that split a before-picture.

### Claims in earlier versions of this file that were wrong

Recording against a live game disproved them. They are corrected in the tables
below, and listed here so that nobody reinstates them:

- `promote_governor` needs a spare **governor point**, not merely an appointed
  governor with promotions listed. `CANNOT_PROMOTE|No governor points
  available` in all four scenarios.
- `attack` and `city_attack` need a **war**. Turn 73 holds an enemy scout in
  range of a city, but the call is refused with `NOT_AT_WAR`. Range matters
  too: that scout was `OUT_OF_RANGE|Target is 3 tiles away (city attack range
  is 2)`.
- `spread_religion` needs the Missionary **in or adjacent to a city**. Turn 73
  has one, at (46,7), two tiles from the nearest city.
- `respond_to_diplomacy` **does** need a save, and an earlier version of this
  file wrongly said otherwise. Recording
  `diplomacy_action(DIPLOMATIC_DELEGATION)` looked like it created the
  precondition, because one run left an encounter pending that blocked
  `end_turn`. It does not: the AI accepted the delegation outright
  (`ACCEPTED|Georgia accepted your delegation`) and the follow-up was refused
  with `NO_SESSION`. The pending encounter in that run came from the AI's own
  initiative, which cannot be produced on demand.
- A **claimable Great Person** needs no save for `patronize`. Turn 73 holds
  349 faith against a 290-faith cost, and the unit that creates covers
  `get_great_person_sites`. Only `recruit` still needs a save, because it
  spends Great Person points rather than faith.

## The saves I need

Four saves close every remaining gap. Each is a checklist. Check the conditions
in the game before you save: several are cleared by ending a turn.

### Save 1 — a war in progress

The largest unlock. Aim for turn 140 or later, at war with a major civ.

| Condition | It unblocks |
|---|---|
| A captured or disloyal city awaiting your decision | `resolve_city_capture`. Stage 2 could only prove that this fails by name |
| An enemy city you can capture on the next turn | Stage 3.4a. A city id encodes its owner, so capturing changes it. No save has ever shown that happen |
| An enemy unit within 2 tiles of one of your cities | `attack` and `city_attack`, and so the general `attack` tool Stage 4 merges them into |
| Past the peace cooldown | `propose_peace` |
| A pillaged improvement | `builder_work(work="repair")` |
| A pillaged district | The `type@x,y` output that Stage 3.4d added and nothing has run |
| A damaged unit | `unit_stance(stance="heal")` |
| A unit with a promotion available | `promote_unit`, and the eligibility marker in Stage 4.9 |
| A spare governor point | `promote_governor`, and `appoint_governor(city_id=…)`, which appoints and assigns in one call |
| An Encampment district of your own | Stage 4.1 asks whether an encampment can attack. Nothing can answer it today |
| A Missionary or Apostle standing in or next to a city | `spread_religion` |
| A Great Person with movement left, on or beside its matching district | `unit_action(activate)` and `activate_great_person`. A patronized Great Person spawns on the city centre with 0 moves, so it cannot reach its district before the recording ends |

A war produces most of this by itself. The capture row is the one to plan for:
leave an enemy city at low health rather than taking it, so the capture happens
inside the recording.

### Save 2 — specialist units

Units no save has ever held. Any era after the Renaissance will do, and one
save can hold all of them at once.

| Condition | It unblocks |
|---|---|
| A Spy, idle, not in transit | `spy_mission` and its 9 missions, and `send_unit_to_city` for a spy. `spy_action` is the one tool that has been in the recording plan since the suite was built and has never once been recorded |
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

### Save 4 — a World Congress in session

The congress runs for one turn every thirty or so, and the resolution hash a
vote needs is printed only while it is sitting. Between sessions the read lists
upcoming policies without one, so the tool cannot be called at all.

| Condition | It unblocks |
|---|---|
| The World Congress in session, with a resolution to vote on | `queue_world_congress_votes` |

Turn 37 reports "World Congress: FIRES THIS TURN", which is not the same thing:
the session opens as that turn ends. Save during the session itself.

## States that a turn boundary destroys

These six are transient. They cannot be reached by playing to a turn number,
because ending a turn clears them. Save at the moment you see one, in whichever
game you are already playing — any of the saves above can carry it.

| Condition | It unblocks |
|---|---|
| Envoy tokens available | `send_envoy` |
| An era about to turn, with a dedication to choose | `choose_dedication` |
| A declared friendship, so an alliance is legal | `form_alliance` |
| Enough Great Person points to recruit | `great_person_action(action="recruit")` |
| A deal offered to you by an AI, unanswered | `respond_to_deal` |
| A diplomacy encounter the AI opened, unanswered | `respond_to_diplomacy`. It blocks `end_turn` until answered, so it is easy to notice and easy to save at |

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
5. Tell me which conditions above the save meets. I will add the plan entries
   and record it.

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
