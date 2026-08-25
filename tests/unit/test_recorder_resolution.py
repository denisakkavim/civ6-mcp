"""Tests for the placeholder resolution in scripts/record_game_traffic.py.

The recorder picks the unit, city and item that each recorded call acts on by
scraping narration text. That is fragile in a way nothing else in the suite is:
narration changes for good reasons, and a scraper that quietly picks the wrong
unit produces a corpus that looks fine and records the wrong thing.

Stage 3.4d is the worked example. Giving enemy units an id in the threat scan
made `get_units` output contain ids that are not ours, and `$EXPENDABLE` — the
unit the `delete` verb destroys — resolved to a Georgian warrior. The recorder
had no opinion about it.
"""

from __future__ import annotations

import importlib.util

from utils import paths

from civ_mcp import ids

_spec = importlib.util.spec_from_file_location(
    "record_game_traffic", paths.REPO_ROOT / "scripts" / "record_game_traffic.py"
)
recorder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recorder)


UNITS_WITH_THREATS = """14 units:
  Scout (UNIT_SCOUT) at (50,11) — CS:10 moves 3/3 [id:0]
  Warrior (UNIT_WARRIOR) at (53,18) — CS:20 moves 3/3 [id:1]
  Builder (UNIT_BUILDER) at (43,11) — moves 2/2 charges:3 [id:2]
    >> Can build: IMPROVEMENT_MINE
  Slinger (UNIT_SLINGER) at (41,11) — CS:5 RS:15 moves 2/2 **CAN UPGRADE to UNIT_ARCHER (30g)** [id:5]

Nearby threats (2):
  Georgia (2 units):
    UNIT_SPEARMAN at (54,20) — CS:25 HP:100/100 (2 tiles away) [id:65546]
    UNIT_SCOUT at (43,5) — CS:10 HP:100/100 (3 tiles away) [id:65541]
"""

OURS_ONLY = UNITS_WITH_THREATS.split("Nearby threats")[0]


def test_enemy_units_are_not_offered_as_our_own():
    """The regression. Every id the recorder acts on must belong to us."""
    contaminated = recorder._military_units(UNITS_WITH_THREATS)
    assert any(ids.owner_of(uid) != 0 for uid in contaminated), (
        "the sample no longer contains an enemy id, so this test proves nothing"
    )

    ours = recorder._military_units(OURS_ONLY)
    assert ours, "the sample should still yield our own combat units"
    assert all(ids.owner_of(uid) == 0 for uid in ours)
    assert all(ids.kind_of(uid) == ids.UNIT for uid in ours)


def test_an_upgradeable_unit_is_found_by_its_marker():
    assert recorder._upgradeable_unit(OURS_ONLY) == 5


def test_no_upgradeable_unit_reports_none_rather_than_guessing():
    without = "\n".join(
        line for line in OURS_ONLY.splitlines() if "CAN UPGRADE" not in line
    )
    assert recorder._upgradeable_unit(without) is None


CITIES = """4 cities:
  Qusqu (pop 6) at (45,12) — Food 10 | Building: BUILDING_SHRINE (2 turns) [id:16777216]
    Districts: DISTRICT_HOLY_SITE(46,12)
  Wanuku (pop 5) at (44,8) — Food 15 | Building: DISTRICT_CAMPUS (7 turns) [id:16777217]
    >> CAN ATTACK: UNIT_SCOUT@46,6(100hp)[65541]
"""


def test_an_attack_target_is_paired_with_the_city_that_can_reach_it():
    """The attacking city must be the one the CAN ATTACK line sits under."""
    assert recorder._attack_targets(CITIES) == (16777217, [(46, 6)])


def test_attack_targets_collects_a_second_tile_for_a_second_district():
    """A city fires once per defended district, so one tile is not enough.

    The Encampment shoots from its own square, so its target is usually a tile
    the City Center cannot reach.
    """
    two = CITIES + "    >> CAN ATTACK: UNIT_ARCHER@47,7(80hp)[65542]\n"
    assert recorder._attack_targets(two) == (16777217, [(46, 6), (47, 7)])


def test_no_attack_target_reports_none():
    assert recorder._attack_targets(CITIES.split(">> CAN ATTACK")[0]) is None


RESEARCH = """Researching: Apprenticeship (TECH_APPRENTICESHIP)
Civic: Defensive Tactics (CIVIC_DEFENSIVE_TACTICS)

Available techs:
  Apprenticeship (TECH_APPRENTICESHIP) [MEDIEVAL] — 40%, 4 turns
  The Wheel (TECH_THE_WHEEL) [ANCIENT] — 0%, 3 turns

Available civics:
  Defensive Tactics (CIVIC_DEFENSIVE_TACTICS) [CLASSICAL] — 0%, 8 turns
  Military Training (CIVIC_MILITARY_TRAINING) [CLASSICAL] — 0%, 6 turns
"""


def test_the_civic_already_running_is_not_chosen():
    """Setting the running civic records ALREADY_COMPLETED, not the write path."""
    assert recorder._researchable_civic(RESEARCH) == "CIVIC_MILITARY_TRAINING"


def test_the_tech_already_running_is_not_chosen():
    assert recorder._researchable_tech(RESEARCH) == "TECH_THE_WHEEL"


POLICIES = """Government: Classical Republic (GOVERNMENT_CLASSICAL_REPUBLIC)

4 policy slots:
  Slot 0 (Economic): Urban Planning (POLICY_URBAN_PLANNING)
  Slot 1 (Economic): Caravansaries (POLICY_CARAVANSARIES)
  Slot 3 (Wildcard): Inspiration (POLICY_INSPIRATION)

Available policies:
  Discipline (POLICY_DISCIPLINE) — +5 combat strength
"""


def test_a_wildcard_slot_is_preferred_because_it_accepts_any_policy():
    assert recorder._slottable_policy(POLICIES) == (3, "POLICY_DISCIPLINE")


GREAT_PEOPLE = """8 Great People:
  Great General: Timur (Medieval Era) — Unclaimed — your points: 0/60
    Patronize: 1100g / 750f
    (individual_id: 176)
  Great Merchant: Marcus Licinius Crassus (Classical Era) — Unclaimed — your points: 16/30
    Patronize: 410g / 290f
    (individual_id: 76)
"""


def test_a_great_person_is_chosen_only_when_the_faith_is_already_banked():
    assert recorder._affordable_great_person(GREAT_PEOPLE, faith=349) == 76
    assert recorder._affordable_great_person(GREAT_PEOPLE, faith=100) is None


def test_the_first_affordable_candidate_wins_not_the_first_listed():
    """Timur heads the list at 750 faith; Crassus is the one we can afford."""
    assert recorder._affordable_great_person(GREAT_PEOPLE, faith=800) == 176
