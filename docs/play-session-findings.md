# Findings from playing Civ 6 through the MCP tools

An agent played this repo's own MCP surface for six turns of a real game, from
turn 355 to turn 361. It made four save files. It also recorded every point
where a tool was hard to use, or reported something untrue.

This document lists what it found. Each item says what happens, what the
evidence is, where the code is, and what to do. Fix them in the order given:
the first group causes the most damage.

Read `tool-surface-review.md` for the design goal. In short: the agent must
pick the right tool and call it correctly the first time.

## How to reproduce any of this

Load a save and drive the server directly. There is no CLI.

```python
# /tmp/civ_drive.py — uv run python /tmp/civ_drive.py TOOL '<json>' [TOOL '<json>']
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, "src")
import civ_mcp.server as server
from civ_mcp.connection import GameConnection
from mcp.shared.memory import create_connected_server_and_client_session

async def main(calls):
    conn = GameConnection()
    with server.testing_overrides(connection_factory=lambda: conn,
                                  background_services=False, log_dir=Path("/tmp/civlogs")):
        async with create_connected_server_and_client_session(
                server.mcp, raise_exceptions=False) as client:
            for name, args in calls:
                print(f"\n=== {name}({json.dumps(args)}) ===", flush=True)
                r = await client.call_tool(name, args)
                print("".join(b.text for b in r.content if getattr(b, "text", None)), flush=True)

argv = sys.argv[1:]; parsed=[]; i=0
while i < len(argv):
    parsed.append((argv[i], json.loads(argv[i+1]) if i+1 < len(argv) else {})); i += 2
asyncio.run(main(parsed))
```

Put several calls in one invocation. FireTuner accepts one connection at a
time and refuses an immediate reconnect.

`load_game` works only when the game is in a match, or at the true main menu.
From any other screen it fails and cannot recover. Always confirm the turn
number with `get_game_overview` after a load.

The saves are in `tests/data/saves/`. `0T_BARB_ROYALSOCIETY` (turn 361) holds
the most: the Royal Society building, idle spies, a spy inside an enemy city,
a Great Prophet, and a Great Scientist on its own Campus.

## Group 1 — writes that report the wrong outcome

This is the most damaging group. The agent cannot detect these. It believes
the report, acts on it, and the game state does not match.

### 1.1 Mutating tools do nothing when the turn is not active, and report success

The agent bought three items while its turn was inactive:

```
purchase_item(BUILDING_GOV_SPIES) -> PURCHASED|BUILDING_GOV_SPIES|cost=1160g (had 4046g)
purchase_item(UNIT_SPY)           -> PURCHASED|UNIT_SPY|cost=1500g (had 4046g)
purchase_item(UNIT_BUILDER)       -> PURCHASED|UNIT_BUILDER|cost=455g (had 4046g)
```

Gold stayed at 4046. No unit appeared. `Players[0]:IsTurnActive()` was false.

The cause is an `ExclusivePopupManager` popup. `server.py` line 425 calls
`gs.ensure_no_blocking_popup()` for every tool marked `mutating=True`. That
guard runs `dismiss_popup(deep=False)`, which by its own docstring does not
reach those popups.

**Fix:** check `IsTurnActive()` in the mutating path and return a clear error.
No code in `src/` calls `IsTurnActive` today. One check turns three silent
failures into one honest message.

### 1.2 `diplomacy_action` reported an acceptance nothing had checked

**Already fixed.** Recorded here because the same pattern may exist elsewhere.

`build_send_diplo_action` in `lua/diplomacy.py` printed
`OK:ACCEPTED|<civ> accepted your friendship declaration` with no state check.
A refusal and an agreement produced the same bytes. The agent built a save
around a friendship that did not exist.

The `_WAR` branch in the same function shows the correct shape. It checks
`IsAtWarWith` and prints `WARN:WAR_UNCERTAIN` when it cannot confirm.

### 1.3 `form_alliance` reports `REJECTED` for an alliance that formed

```
form_alliance({"player_id": 4, "alliance_type": "RESEARCH"})
  -> REJECTED|Georgia rejected the RESEARCH alliance proposal
get_deal_options({"player_id": 4})  -> Agreements: Alliance: RESEARCH (active)
get_diplomacy()                     -> Georgia (Tamar) — ALLIED (+30) (RESEARCH alliance Lv1)
```

The code reads the deal state in the same frame as it closes the session. The
docstring of `build_diplomacy_respond` states the rule: "Caller must check
session state in a SEPARATE call to allow the engine time to process the
response (same-frame checks see stale state)."

**Fix:** apply that rule here. Report `SENT` when the state is not yet
readable, and name the read that confirms it.

### 1.4 `end_turn` reported two turn advances for one

`end_turn` printed `Turn 357 -> 358`, then later `Turn 358 -> 359`.
`Game.GetCurrentGameTurn()` went from 357 to 359. One report was of a turn
that did not happen.

### 1.5 `sacrifice_charges` reported a failure that had succeeded

```
WARN:SACRIFICE_UNCERTAIN|Command sent but charges unchanged (4). ...
  Ensure builder is on the exact district tile where the project's district is located.
```

The builder was consumed and all four charges were spent. The next
`get_units` had no such unit. The advice describes a problem that did not
exist.

**Fix:** read the charge count after the engine has processed the command, or
report `SENT` and name the read that confirms it.

### 1.6 `purchase_item` quotes a stale gold balance

Two purchases in one invocation both printed `(had 4046g)`.

## Group 2 — reads that contradict each other

The agent cannot tell which read is correct. It has to pick one.

### 2.1 Three reads disagree about religion

In one game state:

| Tool | Says |
|---|---|
| `get_game_overview` | `Religion: NONE — all 3 slots filled` |
| `get_belief_options` | `No religion founded. Faith: 3313`, `Available religions (9)`, and advises `found_religion(...)` |
| `get_victory_progress` | `!! All 3 religion slots filled — no more Great Prophets available` |

Only the third is correct. Two reads point at a path the engine has closed.
`get_great_people` also sold the agent a Great Prophet for 350 faith, which
could never found a religion.

The two messages are in `narrate.py` at lines 62 and 1803.

**Fix:** make `get_belief_options` state that founding is closed when all
slots are filled. Check whether `get_great_people` should mark a prophet that
cannot found.

### 2.2 `get_game_overview` and `get_research_options` disagree

`get_game_overview` said `Research: Nuclear Fission`. Two calls later
`get_research_options` said `Researching: Lasers`.

### 2.3 `get_research_options` lists one civic twice, with different numbers

Header: `Civic: Totalitarianism (2 turns)`. List:
`Totalitarianism (CIVIC_TOTALITARIANISM) [MODERN] — 0%, 14 turns BOOSTED`.
It took 2 turns.

### 2.4 `get_cities` advertises attack targets the attack refuses — FIXED

Found while verifying Stage 4's `attack`, against `0T_BARB_WAR2` at turn 117.
Fixed while adding the Encampment branch: the list is now built from each
defended district's own `GetCommandTargets`, intersected with tiles holding a
hostile unit and filtered by the same `CanStartCommand` the write runs. A
`CAN ATTACK` line now means the city can strike that tile right now. It also
covers the Encampment's reach, which the old radius scan could not see at all.


`get_cities` printed, under Methone at (17,14):

```
    >> CAN ATTACK: UNIT_ARCHER@18,16(100hp)[65540]
    >> CAN ATTACK: UNIT_ARCHER@15,17(100hp)[65542]
```

and under Heidelberg at (22,10):

```
    >> CAN ATTACK: UNIT_SCOUT@22,7(43hp)[4128809]
    >> CAN ATTACK: UNIT_SCOUT@25,10(100hp)[4128779]
```

Every one of those calls is refused:

```
attack(attacker_id=16777219, target_x=15, target_y=17)
  -> Error: OUT_OF_RANGE|Target is 3 tiles away (city attack range is 2)
attack(attacker_id=16777220, target_x=22, target_y=7)
  -> Error: OUT_OF_RANGE|Target is 3 tiles away (city attack range is 2)
```

Three of the four listed targets are out of range. The read and the write
disagree about how far a city can shoot. One of them is wrong, and the agent
cannot tell which without spending a call to find out.

The remaining in-range target fails differently — see 5.1 below.

The `CAN ATTACK` list is built in `lua/cities.py`; the range check is
`Map.GetPlotDistance` against `dist > 2` in `build_attack_from_city`. Whichever
is right, both must use it.

## Group 3 — reads that do not feed writes

The review calls this the round-trip rule in section 4j. A read must print
what the matching write accepts.

### 3.1 No read lists the governments `set_government` accepts

`set_government` takes a `GOVERNMENT_*` string. `get_policies` prints only the
current government. `build_available_governments_query()` exists in
`lua/governance.py` line 609, is exported from `lua/__init__.py` line 95, and
**no tool calls it**. The agent used `run_lua` with `IsGovernmentUnlocked` to
find that Fascism was available.

**Fix:** add the available governments to `get_policies` output, or give the
existing query a tool.

### 3.2 Production notifications name no city

`end_turn` and `get_notifications` printed this seven times:

```
* Choose Production -> Use: set_city_production(city_id=..., item_type=...)
```

The literal text `city_id=...` is in `lua/notifications.py` line 154. The
agent had to call `get_cities` and read each line for `Building: nothing`.
That is four calls for one intent.

**Fix:** put the city id in the notification. The notification knows it.

### 3.3 `get_production_options` does not refresh inside a turn

Directly after `PURCHASED|BUILDING_GOV_SPIES`, the same building was still
listed as buyable, and a newly unlocked building was absent. An agent that
trusts this read buys the same building twice.

### 3.4 `get_units` can never report that a unit can promote

Found before the play session, and recorded here because Stage 4.9 depends on
it. `narrate.py` prints `**NEEDS PROMOTION**` when `UnitInfo.needs_promotion`
is true. That field is always false: `lua/units.py` sets `local promo = "0"`
and never changes it. The comment above it gives the reason, and the reason is
sound — an XP check (`GetExperiencePoints() >= GetExperienceForNextLevel()`)
stays true after `SetPromotion()`, so it fires one turn early and causes
double promotions.

The result is a marker that cannot appear. The agent must call
`get_unit_promotions` for each unit to find out, or miss promotions.
`barbpantheon` holds a Scout with two promotions waiting, and `get_units`
says nothing about it.

Stage 4.9 asks `get_units` to mark promotion eligibility. The only correct
source is the GameCore `CanPromote` check that `end_turn` already uses. This
is not a one-line change.

### 3.5 `get_great_person_sites` says "needs move" at distance 0

With the unit standing on the tile, the output was
`Pella (21,17) — needs move (dist 0)`. Distance 0 means the unit can act now.

## Group 4 — documents that describe tools wrongly

### 4.1 `sacrifice_charges` — the docstring named the wrong object

**Already fixed.** The docstring said "Royal Society card". The code checks
`BUILDING_GOV_SCIENCE`, a tier-3 Government Plaza **building**. The agent
searched for `POLICY_ROYAL_SOCIETY`, which does not exist, and read
`get_policies`, which can never list it.

Three preconditions were also unstated, and each one blocks the call:

1. The Royal Society building must be built.
2. The city that owns the district tile must be producing a project.
   Without one the call fails with `ERR:NO_PROJECT`.
3. The building only appears once the player holds a tier-3 government. Under
   Merchant Republic, `get_production_options` omitted it and
   `purchase_item` said `Error: CANNOT_PURCHASE|unknown`. After
   `set_government(GOVERNMENT_FASCISM)` the same read listed it at once.

The game also limits this to once per city per turn.

### 4.2 `run_lua` context guidance predicts the wrong context

`Game.GetReligion():GetNumReligionsStillToFound()` and
`pCulture:IsGovernmentUnlocked()` both fail in `gamecore` with
`function expected instead of nil`. Both work in `ingame`. The docstring says
`gamecore` is for `Game.*` calls, which predicts the opposite.

### 4.3 `run_lua` asks for a sentinel and then removes it

The docstring says to end the code with `print("---END---")`. The agent did
so four times. The sentinel never appeared in the output, because the tool
adds its own. The output looks truncated.

### 4.4 Two call shapes for one intent

`narrate_spies` prints
`Travel: spy_action(unit_id, action='travel', target_x, target_y)`.
The `spy_action` docstring says `city_id: Target city ... Preferred.`

## Group 5 — errors that do not say what to do next

### 5.1 `CANNOT_ATTACK|unknown reason` for a target the same read offered — FIXED

Same root cause as 2.4, arriving at the write side: the read offered a target
the engine had never agreed to. With the read fixed, reaching this branch means
the state moved between read and write, and the message now says so instead of
"unknown reason".


Same session as 2.4. Methone's one remaining `CAN ATTACK` target is in range,
and the attack still fails:

```
attack(attacker_id=16777219, target_x=18, target_y=16)
  -> Error: CANNOT_ATTACK|City cannot attack this target (unknown reason)
```

The Lua reached that branch, so it had already passed both earlier checks:
the target is within 2 tiles, and it *is* in
`CityManager.GetCommandTargets(pCity, RANGE_ATTACK)`. Only
`CanStartCommand` refused. Two units stand on (18,16), a builder and an
archer, and the enemy-selection loop above takes the last one it finds rather
than the first — worth ruling out before looking further.

### Errors whose cause is already on the line

- `Error: CANNOT_PURCHASE|unknown`. The reason is knowable: the player holds
  no tier-3 government.
- `Error: CANNOT_SACRIFICE|unknown. Builder at (51,16) on DISTRICT_CITY_CENTER
  with 3 charges, city building PROJECT_ENHANCE_DISTRICT_COMMERCIAL_HUB`. The
  appended facts are good. The cause follows from them: the builder stands on
  a City Center, not on the Commercial Hub the project belongs to. The message
  still says "unknown".
- `get_dedications` does not say when no choice is open. At turn 355 it
  printed three lines and ended with `Active dedications: ...`. It said
  nothing about whether `choose_dedication` was legal. `narrate_dedications`
  prints "No dedications available or required." only when there are also no
  active dedications, so the common case is silent. The parser reads
  `selections_allowed` and then discards it.

## What worked

Record this too, so nobody removes it.

**The composite ids from Stage 3.4 worked with no mistakes.** Every `[id:N]`
from `get_units` and `get_cities` fed straight into a write. The agent never
confused a unit id with a city id.

**`get_production_options` output pasted into writes without change.** Every
value it printed was accepted by `set_city_production` and `purchase_item`.

## Note on test coverage

Four saves were added to `tests/data/saves/`:
`0T_BARB_ROYALSOCIETY`, `0T_BARB_SPY`, `0T_BARB_FRIENDSHIP`, `0T_BARB_PROPHET`.

They are not yet in `SCENARIO_SAVES` in
`tests/integration/test_recorded_tool_calls.py`, and nothing records them.
Registering and recording them adds coverage for `spy_action`,
`sacrifice_charges`, `form_alliance` and `unit_action(activate)`.

`0T_BARB_PROPHET` is superseded by `0T_BARB_ROYALSOCIETY`. Delete it unless
the just-created prophet state is wanted.
