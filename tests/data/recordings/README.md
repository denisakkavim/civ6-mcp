# Recordings

A recording holds the FireTuner traffic from one tool call. It contains the
Lua exchanges that the tool made, in order, and the text that the tool
returned.

A recording is evidence about the code that wrote it. It shows that a tool
still says what it said. It cannot show that two tools agree with each other:
during Stage 3 four tools printed raw game ids while the rest printed
composite ids, and every recording still replayed clean, because each one
agreed with itself.

A hand-written response line states a belief about what the game sends. If the
belief is wrong, the test passes and the game fails. A recording removes the
belief.

## What is in this folder

There are four scenarios, one per save in `tests/data/saves/`:

There are fourteen scenarios, one per save in `tests/data/saves/`. The four
Inca saves carry the main corpus; the Barbarossa saves each hold one game state
the Inca saves never reach, and are named for that state rather than for a
turn. `tests/integration/test_recorded_tool_calls.py` maps every scenario to
its save and says what each one is for.

Every recording holds a successful call. The recorder deletes a recording when
the game refuses the call, and prints the reason.

One save cannot hold every game state, so the corpus collects tools and verbs
across the four scenarios. `tests/data/saves/WISHLIST.md` lists the states that
no save holds, and names the tool or the verb that each state blocks.

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

The `dispatchers` subset covers the eleven unit tools that Stage 4 split out of
`unit_action`, `city_attack`, `spy_action` and `skip_remaining_units`. Those
four tools no longer exist, so their recordings were deleted with them: a
recording cannot replay through a tool that is gone.

Each scenario must be recorded against its own save.
`tests/integration/test_recorded_tool_calls.py` maps each scenario to its save,
and fails if the save is missing.

**Verify the loaded turn before you record.** The OCR load fails sometimes. It
reports `FAILED: Could not find 'Load Game' button` and leaves the previous
game running. The recorder does not check, so it will record the wrong game
under the new scenario's name. Call `get_game_overview` after the load and
compare the turn number. This happened during Stage 3 and produced 36
recordings of a turn-74 game inside `turn37/`.

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
