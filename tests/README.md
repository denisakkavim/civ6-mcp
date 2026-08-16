# Tests

## How to run the tests

Run the offline tests. CI runs this command:

```bash
uv run pytest -m "not live"
```

Run the tests that need a game. Start Civ 6 with `EnableTuner=1` and load a
save first:

```bash
uv run pytest -m "live and not destructive"
```

Run the test that restarts the game. Run it on its own:

```bash
uv run pytest -m "live and destructive"
```

Update the snapshots after an intended change. Read the diff before you commit
it:

```bash
uv run pytest --update-snapshots
```

## Folder layout

There are four test folders. Each folder needs more than the folder above it.
If a test needs more than its folder allows, move the test down one level.

| Folder | The test needs | The tests check |
|---|---|---|
| `unit/` | nothing | the logic in one function or method |
| `contract/` | nothing | that the tool surface can be called correctly |
| `integration/` | recorded traffic | the full stack, from MCP call to text |
| `e2e/` | a running Civ 6 | that the Lua code is correct |

```
tests/
  conftest.py              shared fixtures
  utils/                   helper code, not tests
    paths.py               all paths, resolved once
    snapshots.py           compares and rewrites snapshots
    recordings.py          Recording and ReplayConnection
  unit/
  contract/
  integration/
  e2e/
  data/
    recordings/<scenario>/ recorded game traffic, one file per tool call
    snapshots/             expected output
    saves/                 the Civ 6 saves that the recordings came from
```

Every test can import the helper code as `from utils import recordings`. The
`pythonpath = ["tests"]` option in `pyproject.toml` makes this work.

## The two test seams

`conftest.py` gives you two ways to drive the code.

**`make_game_state`** calls a `GameState` method directly. You supply the Lua
responses. This seam is fast. Use it for logic that is inside one method. The
tests in `unit/` use it.

**`civ_server`** calls the real `FastMCP` server through an in-memory client
session. The call goes through argument validation, the tool body,
`GameState`, the `lua/` builders, the parsers, and `narrate.py`. This seam is
the only one that runs the code in `server.py`. The tests in `integration/`
use it.

```python
def test_something(civ_server):
    result = civ_server(SomeFakeConnection()).call("get_units", {})
    assert "UNIT_WARRIOR" in result
```

A tool error is not a protocol error. `_logged` catches `LuaError`,
`ValueError`, and `ConnectionError`. It returns the message as text, and the
call succeeds. Assert on the text. Use `call_raw` when you must check
`isError`.

## Snapshots

`data/snapshots/tool_schemas.json` holds the name, description, and schema of
every tool. The snapshot does not show whether the surface is correct. It
shows each change as a diff. The refactor plan lists the surface changes in
tables. Each stage must produce a diff that matches its table.

`data/snapshots/narration/` holds the text that each recording produces.

To accept a change, run `pytest --update-snapshots`. Then read the diff. Do
not update a snapshot only to make the tests pass. Snapshots exist to prevent
this. CI fails if you update a snapshot and do not commit it.

## Recordings

`data/recordings/` holds 181 recordings across four scenarios: 43 at turn 37,
45 at turn 57, 46 at turn 63, and 47 at turn 73. Each scenario came from the
save of the same name in `data/saves/`. One save cannot hold every game state,
so the corpus collects tools and verbs across the four.

A replay drives the real tool and supplies the recorded responses instead of
the game. To make more recordings, read `data/recordings/README.md`.

Re-record a scenario when the Lua changes what it prints. Stage 3 did this
twice. **Verify the loaded turn before you record.** The OCR load fails
sometimes, leaves the previous game running, and the recorder will then record
that game under the new scenario's name.

The replay matches responses by position within each context. It does not
match on the Lua text. A recording therefore survives a change to the Lua that
a tool sends. It fails only when the number or the order of the queries
changes.

`end_turn` is recorded but not replayed. It polls for the AI turn on a wall
clock. Its query count depends on elapsed time, not on game state.

## What the contract tests are for

The goal of the refactor is that the agent picks the right tool and calls it
correctly the first time. The tests in `contract/` are the closest the suite
gets to measuring that. Each one closes a way to call a tool wrongly:

- A closed set of values must appear as an `enum` in the schema. Otherwise the
  agent finds the legal values by calling the tool wrongly.
- A tool with parameters must mark at least one as required. Otherwise an
  empty call is well formed, and the agent learns the requirement by failing.
- A read must print type strings that a write accepts.
- Every `get_*` tool must carry `readOnlyHint`.
- Every tool name inside agent-facing text must exist.

Size is checked too, but it is a symptom and not the goal. A tool over the
per-tool ceiling usually holds a rule in prose that belongs in its schema. A
change that grows the surface and removes a way to call a tool wrongly is a
good change: raise the constant and write down why. Do not cut a real
precondition to meet a number.

No `xfail(strict=True)` markers remain. They tracked open stages of the
refactor, and every stage they tracked has landed.

## Saves

`data/saves/` holds the save that each recording scenario came from. You
cannot re-record a scenario without its save. Install the saves with this
command:

```bash
uv run python scripts/install_saves.py
```

`data/saves/README.md` explains why the CivBench scenario saves are not here.
