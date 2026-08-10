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

# A tool schema past this size is almost always prose that belongs in the
# schema itself — `unit_action` is 2,610 chars mostly because its 20 verbs and
# their parameter rules are written out in the docstring. Ratchet this down as
# the refactor lands; never up.
MAX_TOOL_SCHEMA_CHARS = 2700
MAX_SURFACE_CHARS = 43_000


def _tools():
    return asyncio.run(mcp.list_tools())


def _tool_size(tool) -> int:
    return (
        len(tool.name)
        + len(tool.description or "")
        + len(json.dumps(tool.inputSchema))
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
# Size budget — the review's actual metric
# ---------------------------------------------------------------------------


def test_no_single_tool_schema_is_oversized():
    oversized = {
        tool.name: _tool_size(tool)
        for tool in _tools()
        if _tool_size(tool) > MAX_TOOL_SCHEMA_CHARS
    }
    assert not oversized, (
        f"Tool schemas over {MAX_TOOL_SCHEMA_CHARS} chars: {oversized}. "
        f"Move the rules out of the docstring and into the schema."
    )


def test_total_surface_size_does_not_grow():
    """A ratchet, not an exact figure.

    The review's goal is a smaller surface at roughly flat tool count, so the
    direction that matters is growth. Lower this constant when a stage lands.
    """
    total = sum(_tool_size(tool) for tool in _tools())
    assert total <= MAX_SURFACE_CHARS, (
        f"Tool surface grew to {total} chars (budget {MAX_SURFACE_CHARS}, "
        f"~{total // 4} tokens). The agent pays this on every call."
    )


# ---------------------------------------------------------------------------
# Contract assertions — properties, not names
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="Stage 1.2: get_religion_beliefs, get_dedications, get_gp_advisor "
    "are pure reads with no readOnlyHint annotation.",
)
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


@pytest.mark.xfail(
    strict=True, reason="Stage 2.4: `list_saves` becomes `get_saves`."
)
def test_tool_names_use_the_get_set_convention():
    """`list_*` in an otherwise `get_*` surface is a stray (§4 verb families)."""
    strays = [t.name for t in _tools() if t.name.startswith("list_")]
    assert not strays, f"`list_*` tools in a `get_*` surface: {strays}"


@pytest.mark.xfail(
    strict=True,
    reason="Stage 1.3 types the dispatcher discriminators with Literal; "
    "Stage 4 splits them. Until then action/focus/votes are bare values.",
)
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
        if discriminator in properties:
            spec = properties[discriminator]
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

# Parameters the review identified as closed sets typed as bare `str`. Each
# becomes a `Literal` in Stage 1.3; the tool names on the left change in
# Stage 2 and again in Stage 4, so update this table as those land.
# Every one is `xfail(strict=True)`: the check is correct today and the code is
# not. When Stage 1.3 types a parameter, its entry XPASSes and the suite goes
# red — which is the prompt to delete the marker. The list is a checklist that
# cannot rot.
_PENDING = pytest.mark.xfail(strict=True, reason="Stage 1.3: not yet a Literal")

CLOSED_SET_PARAMETERS = [
    pytest.param("unit_action", "action", marks=_PENDING),
    pytest.param("spy_action", "action", marks=_PENDING),
    pytest.param("send_diplomatic_action", "action", marks=_PENDING),
    pytest.param("form_alliance", "alliance_type", marks=_PENDING),
    pytest.param("respond_to_diplomacy", "response", marks=_PENDING),
    pytest.param("set_city_focus", "focus", marks=_PENDING),
    pytest.param("purchase_item", "yield_type", marks=_PENDING),
    pytest.param("patronize_great_person", "yield_type", marks=_PENDING),
    pytest.param("propose_trade", "mode", marks=_PENDING),
    pytest.param("run_lua", "context", marks=_PENDING),
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
