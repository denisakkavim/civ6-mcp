"""The tool surface is the product. This file is what notices when it changes.

Two kinds of check:

- **A snapshot** (`tests/data/snapshots/tool_schemas.json`) of every tool's name,
  description and input schema. It has no opinion about what is right; it makes
  every change visible as a diff. The refactor plan is written as tables of
  surface changes, and this turns each stage into a diff you can read against
  its table.
- **Contract assertions** — properties that must hold whatever the names are,
  so they survive the renames the plan is about to make.

`web/content/tools.json` was the previous attempt at the first of these. It
went stale (missing 4 tools, listing 3 deleted ones) because nothing compared
it to reality. The difference here is that this one fails.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from civ_mcp.server import mcp
from utils import snapshots

# Size is a symptom, not the goal.
#
# The goal is that the agent picks the right tool and calls it correctly the
# first time. Schema size matters twice over, and the second reason is the
# important one:
#
# 1. The agent pays every character on every call, hundreds of turns per game.
# 2. A long docstring is usually a *rule that belongs in the schema*. The
#    review's example is `unit_action` at 2,610 chars, "almost entirely
#    because of this prose" — twenty verbs whose parameter rules JSON Schema
#    cannot express, each one duplicated as a runtime error the agent
#    discovers by failing.
#
# So these ceilings are a detector for (2), and a tax meter for (1). They are
# not the objective. Shrinking a tool by deleting a real precondition makes
# the number better and the surface worse.
#
# **When a change grows the surface and makes the tool easier to call
# correctly, take the change and raise the constant.** Say why in this comment
# and in the plan. What must not happen is a silent raise, or a cut that
# removes something the agent needed in order to hit a number. The properties
# below are the real bar; this is the smoke alarm.
#
# History, kept because each entry says what moved and why:
#
# Stage 1.3 is close to size-neutral by construction: an enum states its values
# in the schema at roughly the length the docstring stated them in prose, so
# what it buys is legibility, not bytes. `unit_action` still fell 2,621 -> 2,328
# and `set_city_production` 1,027 -> 903, but splitting `set_research` into
# `set_tech` / `set_civic` (1.4) spent most of that back on a second tool's
# boilerplate. The large drop is Stage 4, where 20 verbs in one docstring
# become six tools with fixed signatures.
#
# Stage 2 took the surface from 76 tools / 42,330 chars to 69 / 40,425, almost
# all of it from deleting tools rather than shrinking them: the seven
# save/lifecycle and great-person tools that merged, plus `dismiss_popup`. The
# largest schema is now `propose_deal` (2,498), which Stage 4 does not touch —
# it is ten flat scalars, deliberately (see "Considered and rejected").
#
# Stage 3 raised both, the first stage to do so, and the raises were correct.
# It bought calls with characters: an implicit placement tile, a move-then-act
# target with two distinct partial results, batched stance verbs, and an id
# accepted where coordinates were. An agent will not use any of those unless
# the schema says they exist. Splitting `get_map_area` into
# `get_map_area(center_x, center_y)` and `get_map_around(entity_id)` cost 209
# chars and one tool, and put a required argument back where the agent reads
# it — the single clearest case of the trade being worth it.
#
# Stage 3 also shows the failure mode. Trimming `unit_action`'s docstring to
# fit 2,500 cut real preconditions ("not on a route", "must stand on the
# district") to satisfy a number. That was the proxy winning. Most of Stage 3's
# growth sat in `unit_action`, and the expectation was that Stage 4 would take
# it back out. It did not — see the Stage 4 note below.
# Raised again after an agent played a real game through this surface and
# went looking for a policy card that does not exist: `sacrifice_charges` said
# "Royal Society card" when the check is for BUILDING_GOV_SCIENCE, a tier-3
# Government Plaza *building*, gated behind a tier-3 government, and needs the
# city to be producing a project. Four unstated preconditions cost more than
# the 139 characters that state them.
#
# Stage 4 was written expecting the large drop, and it went the other way:
# 42,861 -> 47,087, and the three dispatchers it deleted cost 5,680 chars while
# the eleven tools replacing them cost 9,597. The plan's prediction assumed
# twenty verbs of prose were the cost. They were not. Three things are:
#
# 1. Envelope. Every tool pays ~110 chars of JSON Schema wrapper and its own
#    name and summary line. Seven more tools is ~800 chars before a word of
#    documentation.
# 2. Rules that were stated once are now stated where they apply. The
#    move-then-act contract (MOVED_PARTIAL / ARRIVED_WAITING) governed seven
#    verbs from one paragraph in `unit_action`; it now appears in the three
#    tools that can return it, because an agent reading `found_city` never
#    sees `builder_work`'s docstring.
# 3. Preconditions that a discriminator hid. `unit_action` could not say which
#    of its twenty verbs needed walls, an idle trader or a district underfoot
#    without saying it twenty times, so it mostly did not. Each tool now says
#    its own.
#
# All three are the schema doing its job. What the stage did deliver is the
# thing the size was ever a proxy for: the runtime error branches are gone.
# "move requires target_x and target_y", "attack requires target_x and
# target_y" and BATCH_NOT_ALLOWED were three ways to learn a rule by failing,
# and the schema now refuses those calls before they are sent. The per-tool
# ceiling did not move, which is the check that actually detects prose
# carrying a rule: the largest tool in the split is `builder_work` at 1,737,
# against `unit_action`'s 3,039.
#
# `skip_remaining_units` was deleted by Stage 4 and put back afterwards, for
# 679 chars. §4.3 held that `unit_stance`'s list form made it redundant. It did
# not: the tool runs two Lua loops over the whole roster — fortify or heal the
# combat units, then finish the moves of everything left — where the list form
# is one round trip per unit and fortifies nothing. It is also the only one of
# the two that takes no ids, which is the point: the agent does not have to
# read `get_units` to find out what it is about to settle.
#
# `attack` grew to 1,262 when the Encampment became a real dispatch branch.
# A city shoots once per turn from each defended district, so one city id can
# mean two strikes from two tiles, and an Encampment covers ground the centre
# cannot. None of that is guessable, and an agent that does not know it leaves
# half the city's firepower unused every turn. The docstring also states that
# an enemy Encampment or City Center absorbs a strike aimed at a unit standing
# on it — measured, and the reason a hit can look like it did nothing.
MAX_TOOL_SCHEMA_CHARS = 3050
MAX_SURFACE_CHARS = 47_700


def _tools():
    return asyncio.run(mcp.list_tools())


def _tool_size(tool) -> int:
    return (
        len(tool.name) + len(tool.description or "") + len(json.dumps(tool.inputSchema))
    )


def _surface() -> dict:
    """The whole surface, shaped for a readable diff."""
    return {
        tool.name: {
            "description": tool.description,
            "inputSchema": tool.inputSchema,
            "annotations": (
                tool.annotations.model_dump(exclude_none=True)
                if tool.annotations
                else None
            ),
        }
        for tool in _tools()
    }


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def test_tool_surface_matches_snapshot():
    """Any change to any tool's name, description or schema shows as a diff."""
    snapshots.check_json("tool_schemas.json", _surface())


# ---------------------------------------------------------------------------
# Size — a smoke alarm, not the objective. See the note on the constants.
# ---------------------------------------------------------------------------


def test_no_single_tool_schema_is_oversized():
    """A big tool is usually a tool whose rules the schema does not state."""
    oversized = {
        tool.name: _tool_size(tool)
        for tool in _tools()
        if _tool_size(tool) > MAX_TOOL_SCHEMA_CHARS
    }
    assert not oversized, (
        f"Tool schemas over {MAX_TOOL_SCHEMA_CHARS} chars: {oversized}. "
        f"Usually this means prose is carrying a rule the schema should state "
        f"— move it into the schema, or split the tool by signature. If the "
        f"size buys the agent something it needs, raise the constant and say "
        f"why. Do not cut a real precondition to fit."
    )


def test_total_surface_size_stays_within_budget():
    """The tax meter. Every character is paid on every call.

    Not a ratchet. A stage that makes tools easier to call correctly may cost
    characters — Stage 3 did, deliberately. Raise the constant with a reason
    rather than trimming something the agent needs.
    """
    total = sum(_tool_size(tool) for tool in _tools())
    assert total <= MAX_SURFACE_CHARS, (
        f"Tool surface grew to {total} chars (budget {MAX_SURFACE_CHARS}, "
        f"~{total // 4} tokens). If the growth buys correct calls, raise the "
        f"budget and record why. If it is prose restating a schema rule, move "
        f"it into the schema instead."
    )


# ---------------------------------------------------------------------------
# Can the agent call it correctly? — the properties that are the actual bar.
#
# Each one closes a way to get a call wrong: not knowing the legal values, not
# knowing which arguments are needed, reading a value that the write side will
# not accept, or being unable to tell two tools apart. These survive renames,
# which the size checks and the snapshot do not.
# ---------------------------------------------------------------------------


def test_every_read_tool_is_annotated_read_only():
    """Clients gate on `readOnlyHint`; a pure read without it reads as a mutation."""
    missing = [
        tool.name
        for tool in _tools()
        if tool.name.startswith("get_")
        and not (tool.annotations and tool.annotations.readOnlyHint)
    ]
    assert not missing, f"get_* tools without readOnlyHint: {missing}"


def test_no_tool_takes_a_json_string_parameter():
    """Structured data belongs in the schema, not inside a string (§2e).

    Detects the pattern by description, since the type is plain `str`: a
    parameter whose description mentions JSON is one the model has to
    hand-serialise.
    """
    offenders = []
    for tool in _tools():
        for name, spec in (tool.inputSchema or {}).get("properties", {}).items():
            if spec.get("type") != "string":
                continue
            description = (spec.get("description") or "").lower()
            if "json" in description:
                offenders.append(f"{tool.name}.{name}")
    assert not offenders, (
        f"Parameters carrying JSON inside a string: {offenders}. "
        f"Use a typed list or dict so the schema states the shape."
    )


def test_tool_names_use_the_get_set_convention():
    """`list_*` in an otherwise `get_*` surface is a stray (§4 verb families)."""
    strays = [t.name for t in _tools() if t.name.startswith("list_")]
    assert not strays, f"`list_*` tools in a `get_*` surface: {strays}"


def _declares_a_collection(spec: dict) -> bool:
    """True if the parameter holds a list or an object rather than one value."""
    collections = ("array", "object")
    if spec.get("type") in collections:
        return True
    for variant in spec.get("anyOf", []) or []:
        if variant.get("type") in collections:
            return True
    return False


def test_dispatchers_name_their_discriminator_after_themselves():
    """`<subject>_<discriminator>` carries a parameter of the same name (§6b).

    `unit_stance(stance=…)`, `builder_work(work=…)`, `spy_mission(mission=…)`.
    Reading the tool name should tell you what the discriminator is called.
    """
    violations = []
    for tool in _tools():
        if "_" not in tool.name:
            continue
        discriminator = tool.name.rsplit("_", 1)[-1]
        properties = (tool.inputSchema or {}).get("properties", {})
        # Only tools that actually have a discriminator-shaped parameter are
        # in scope: a tool named for its verb (`move_unit`) is exempt.
        if discriminator not in properties:
            continue
        spec = properties[discriminator]
        # So is a tool whose trailing word names a payload rather than a
        # choice: `queue_world_congress_votes(votes=[…])` is verb-plus-object, and a
        # discriminator is always one value picked from a set, never a list.
        if _declares_a_collection(spec):
            continue
        if not spec.get("enum") and not spec.get("anyOf"):
            violations.append(f"{tool.name}.{discriminator}")
    assert not violations, (
        f"Discriminator parameters typed as bare values rather than a closed "
        f"set: {violations}"
    )


def test_option_and_site_families_are_coherent():
    """`get_*_options` lists choices; `get_*_sites` lists placements (§5)."""
    names = {t.name for t in _tools()}
    options = {n for n in names if n.endswith("_options")}
    sites = {n for n in names if n.endswith("_sites")}
    for name in options | sites:
        assert name.startswith("get_"), (
            f"{name} is in a read-only family but is not a `get_*` tool"
        )


def test_end_turn_takes_no_arguments():
    """Ending a turn is a game action, not a journalling ritual (§1).

    Stage 1.1 removed the five reflection parameters. This keeps them gone.
    """
    end_turn = next(t for t in _tools() if t.name == "end_turn")
    properties = (end_turn.inputSchema or {}).get("properties", {})
    assert properties == {}, f"end_turn regained parameters: {sorted(properties)}"


def test_no_agent_memory_tools():
    """Per-agent memory does not belong in a shared game server (§1)."""
    names = {t.name for t in _tools()}
    assert "get_diary" not in names


@pytest.mark.parametrize(
    "tool_name",
    ["get_units", "get_cities", "get_game_overview", "get_map_area"],
)
def test_core_reads_are_present(tool_name):
    """A rename that loses a core read entirely should fail loudly, not diff."""
    assert tool_name in {t.name for t in _tools()}


# ---------------------------------------------------------------------------
# Closed-set parameters (Stage 1.3)
# ---------------------------------------------------------------------------

# Parameters the review identified as closed sets typed as bare `str`. Stage
# 1.3 typed all of them with `Literal`; Stage 2 and Stage 4 renamed the tools
# on the left, and this table moved with them.
#
# `builder_work.improvement_type` is deliberately absent. Its legal values are a
# property of the game database, not of this server, and the read side emits
# values from that database directly — `get_units` prints what each builder can
# build here, and `get_builder_tasks` can recommend IMPROVEMENT_OIL_WELL. An
# enum hand-written from the review's eight generic improvements would refuse
# values the server's own reads suggest. It gets typed when it is generated
# from GameInfo.Improvements (the review's exit path), not before.
CLOSED_SET_PARAMETERS = [
    # Stage 4 split `unit_action` and `spy_action` by signature. Every
    # discriminator that survived the split is here; the verbs that became
    # tools of their own no longer have one to type.
    ("unit_stance", "stance"),
    ("builder_work", "work"),
    ("disband_unit", "mode"),
    ("spy_mission", "mission"),
    ("diplomacy_action", "action"),
    ("form_alliance", "alliance_type"),
    ("respond_to_diplomacy", "response"),
    ("set_city_focus", "focus"),
    ("purchase_item", "yield_type"),
    ("propose_deal", "mode"),
    ("run_lua", "context"),
    # Stage 2 merged the three great-person verbs into one dispatcher and split
    # the capture branch out of `city_action`; both discriminators are closed
    # sets and belong here.
    ("great_person_action", "action"),
    ("great_person_action", "yield_type"),
    ("resolve_city_capture", "action"),
]


@pytest.mark.parametrize("tool_name,parameter", CLOSED_SET_PARAMETERS)
def test_closed_set_parameters_are_enums(tool_name, parameter):
    """Legal values must be visible in the schema, not learned by failing (§3)."""
    tools = {t.name: t for t in _tools()}
    if tool_name not in tools:
        pytest.skip(f"{tool_name} has been renamed or split; update the table")
    spec = (tools[tool_name].inputSchema or {}).get("properties", {}).get(parameter)
    assert spec is not None, f"{tool_name} has no parameter {parameter}"
    has_enum = bool(spec.get("enum")) or any(
        variant.get("enum") for variant in spec.get("anyOf", []) or []
    )
    assert has_enum, (
        f"{tool_name}.{parameter} is a closed set typed as a bare value; the "
        f"agent can only discover the legal values by calling it wrong."
    )


# Tools whose every parameter is genuinely optional. Keep this short and
# justified: a tool that lands here because two of its arguments are
# alternatives is a tool that should have been split.
OPTIONAL_ONLY_TOOLS = frozenset(
    {
        # Defaults to the most recent autosave, which is the common case and
        # the one you want when the game has hung.
        "restart_game",
    }
)


def test_every_tool_states_what_it_needs():
    """A tool that requires something must say so in its schema.

    Stage 3.4c broke this and nothing caught it. Letting an id stand in for
    coordinates meant making both optional, because JSON Schema cannot express
    "exactly one of these groups" — which left `get_map_area` with no required
    argument at all, so an empty call was well-formed and failed at runtime.
    Splitting it into a tile form and an entity form fixed it. The lesson
    generalises: an either/or parameter pair is a tool wanting to be two tools.
    """
    silent = []
    for tool in _tools():
        schema = tool.inputSchema or {}
        if not schema.get("properties"):
            continue  # genuinely takes nothing
        if schema.get("required"):
            continue
        if tool.name in OPTIONAL_ONLY_TOOLS:
            continue
        silent.append(tool.name)

    assert not silent, (
        f"Tools with parameters but nothing required: {silent}. An empty call "
        f"is well-formed, so the agent discovers the requirement by failing. "
        f"If two arguments are alternatives, split the tool so each states its "
        f"own requirement (see get_map_area / get_map_around)."
    )
