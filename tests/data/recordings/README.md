# Recordings

A recording holds the FireTuner traffic from one tool call. It contains the
Lua exchanges that the tool made, in order, and the text that the tool
returned.

A hand-written response line states a belief about what the game sends. If the
belief is wrong, the test passes and the game fails. A recording removes the
belief.

## What is in this folder

`turn37/` holds 44 recordings from a live game. The game was Inca
(Pachacuti) at turn 37, with 3 cities and 6 units. The recordings were made on
2026-08-10 from the `0T_TURN37_INCA` save. The folder contains:

- 33 read calls.
- 7 dispatcher calls. Stage 4 deletes these tools.
- 4 write calls.

Every recording holds a successful call.

Three tools have no recording, because that save has no unit or city that can
run them:

- `unit_action(heal)` needs a damaged unit.
- `spy_action` needs a spy.
- `city_action` needs a city capture or a ranged attack.

Record these tools from a later-game save. Do not add recordings of failed
calls.

## How to make recordings

1. Install the saves:

   ```bash
   uv run python scripts/install_saves.py
   ```

2. Start Civ 6 with `EnableTuner=1` and load a save.

3. Record the traffic:

   ```bash
   uv run python scripts/record_game_traffic.py --scenario <name>
   ```

The `--subset` option selects part of the plan. The values are `all`, `reads`,
`dispatchers`, and `writes`. The `--show-plan` option prints the calls and
exits. The `--dry-run` option resolves the live ids but writes nothing.

Record the dispatcher calls first:

```bash
uv run python scripts/record_game_traffic.py --subset dispatchers --scenario turn37
```

Stage 4 of the tool surface refactor deletes `unit_action`, `city_action`,
`spy_action`, and `skip_remaining_units`. After the deletion you cannot show
that the eleven new tools behave like the tools they replace.

The `turn37` recordings came from `tests/data/saves/0T_TURN37_INCA.Civ6Save`.
Record against the same save, or the recordings will not agree with each
other. `tests/integration/test_recorded_tool_calls.py` maps each scenario to
its save. The test fails if the save is missing.

Write calls change the game. Reload the save between runs to keep the
scenarios comparable. The full plan records the reads first, then the
dispatchers, then the writes, from one clean load.

`end_turn` is recorded but not replayed. It polls for the AI turn on a wall
clock. Its query count depends on elapsed time, not on game state.
`tests/integration/test_recorded_tool_calls.py` lists it in `NOT_REPLAYABLE`
with this reason.

## File layout

```
data/recordings/<scenario>/<tool>[__<verb>].json
data/snapshots/narration/<scenario>/<tool>[__<verb>].txt
```

Name the scenario after the save that you recorded it from. The `__<verb>`
suffix keeps one recording per dispatcher verb. Without it, each recording
overwrites the last.

## How the replay works

`tests/integration/test_recorded_tool_calls.py` replays each recording through
the real MCP server. It then compares the text against the snapshot.

The replay matches responses by position within each context. It does not
match on the Lua text. The module docstring in `tests/utils/recordings.py`
explains why. A recording therefore survives a change to the Lua that a tool
sends. It fails only when the number or the order of the queries changes.

To accept an intended change to the text, run this command:

```bash
uv run pytest --update-snapshots
```

Read the diff before you commit it. The diff is the reason the snapshot
exists.
