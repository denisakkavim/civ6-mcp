# Test saves

These saves give a fixed start state. The live tests (`pytest -m live`) use
them. The recording script uses them.

Install them into the Civ 6 save directory:

```bash
uv run python scripts/install_saves.py
```

## What is in this folder

| File | Game state |
|---|---|
| `0T_TURN37_INCA.Civ6Save` | Inca (Pachacuti), turn 37, 3 cities, 6 units, Classical era |
| `0T_TURN57_INCA.Civ6Save` | Inca (Pachacuti), turn 57, 3 cities, 10 units, Classical era |
| `0T_TURN63_INCA.Civ6Save` | Inca (Pachacuti), turn 63, 3 cities, 12 units, Medieval era, Shinto founded |
| `0T_TURN73_INCA.Civ6Save` | Inca (Pachacuti), turn 73, 4 cities, Medieval era, idle trader, builder on a buildable tile |

The recordings in `tests/data/recordings/turn37/` came from the first save. A
save is tracked because you cannot re-record a scenario without its save.

Turn 57 holds units and buildings that turn 37 does not have: a settler, a
trader, a builder with charges, city walls, and a Holy Site. It has also met
Georgia. Each of these is the precondition for a `unit_action` verb that turn
37 cannot record. Stage 4 of the tool-surface plan replaces every one of those
verbs, so the recordings are the evidence that the replacements behave the
same.

Turn 63 is the first save with a religion. It is therefore the only save where
`religion_type` holds a value. Every other save shows the fallback to the
display name. Turn 63 also sees the cities of Georgia. Stage 3.4 needs foreign
cities to test the composite city id.

`scripts/record_game_traffic.py` skips a verb when the loaded save cannot meet
its precondition. It prints the reason. One save therefore does not have to
hold everything. Record each scenario from its own save. The corpus collects
the verbs across the scenarios.

These verbs still have no recording. Each one needs a unit or a tile state that
none of the three saves holds:

| Verb | What it needs |
|---|---|
| `repair` | a pillaged improvement |
| `remove_feature`, `remove_improvement` | a builder on a feature or an improvement |
| `build_route` | a Military Engineer |
| `heal` | a damaged unit |
| `activate` | a Great Person on its matching district |
| `spread_religion` | a Missionary or an Apostle next to a city |
| `trade_route` | a Trader that is not on a route |
| `attack`, `city_action` | an enemy unit in range |
| `spy_action` | a Spy |
| `sacrifice_charges` | the Royal Society card |

Some preconditions do not appear in the output of any tool. A Missionary must
stand in or next to a city. A Great Person must stand on its own district.
The recorder cannot test for these, so it makes the call and then deletes the
recording if the game refuses it. It reports each deletion. The corpus
therefore holds no failed call.

The file was an autosave named `0_MCP_0037`. This repository's own server
wrote it during an earlier run. The file was renamed before it was added.
The rename is necessary for two reasons:

- `_auto_boot` in `server.py` deletes every `0_MCP_*.Civ6Save` file in the
  save directory before it loads a scenario. The server would delete the test
  save.
- `autosave.py` treats `0_MCP_*` files as the crash-recovery set. The recovery
  path could load a test save instead of the real game state.

The name keeps the `0` prefix. The Civ 6 Load Game screen sorts by name, so a
leading `0` puts the save at the top of the list. The OCR navigation looks
there.

## Why the CivBench saves are not here

`0A_GROUND_CONTROL`, `0B_SNOWFLAKE`, and `0C_CRY_HAVOC` were the scenarios of
the eval harness. They are not in this folder for two reasons:

1. They need about 15 DLC packs. A machine without that content cannot load
   them. Such a machine therefore cannot run the live tests or record traffic
   with them. Section 7 of `tool-surface-test-plan.md` records how this was
   found.
2. No code uses them. Commit `a091d1d` deleted the inspect-ai harness that
   used them. Only docstring examples name them now.

The files stay in the Git history. To recover them, run these commands:

```bash
git show a091d1d^:evals/saves/0A_GROUND_CONTROL.Civ6Save > tests/data/saves/0A_GROUND_CONTROL.Civ6Save
git show a091d1d^:evals/saves/README.md
```

Recover them on a machine that has the full DLC. Agent-versus-agent runs need
a balanced, documented start position. A save from an old game does not give
one.

## How to add a save

Use a save that this repository's server made on the machine you test on. Such
a save always loads. A curated save may not load.

1. Rename the file so that it does not match `0_MCP_*`.
2. Copy the file into this folder.
3. Add a row to the table above. State the civ, the turn, and the number of
   cities and units.
4. Add the scenario to `SCENARIO_SAVES` in
   `tests/integration/test_recorded_tool_calls.py`. A test fails when a
   scenario has recordings but no save.

A test cannot make good assertions against a save whose contents nobody knows.
