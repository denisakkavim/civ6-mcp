"""Unit tests for Lua response parsers.

Each parser takes list[str] (pipe-delimited lines from Lua print()) and returns
typed dataclasses. These tests verify the parsing logic with realistic fixtures.
"""

import pytest

from civ_mcp.lua.cities import parse_cities_response
from civ_mcp.lua.map import parse_map_response
from civ_mcp.lua.notifications import parse_end_turn_blocking
from civ_mcp.lua.overview import parse_gameover_response, parse_overview_response
from civ_mcp.lua.units import (
    parse_combat_estimate,
    parse_threat_scan_response,
    parse_units_response,
)

# Minimal 19-field overview line: turn|pid|civ|leader|gold|gpt|sci|cul|faith|
#   research|civic|cities|units|score|favor|fpt|pop|gold_income|maintenance
OVERVIEW_LINE = (
    "42|0|CIVILIZATION_INDIA|Gandhi|500.0|10.5|25.0|18.0|12.0|"
    "TECH_POTTERY|CIVIC_CODE_OF_LAWS|3|5|120|10|2|15|35.0|24.5"
)

# Unit fields: uid|index|name|type|x,y|moves/max|hp/max|cs|rs|charges|targets|
#   promo|upgrade|upgrade_target|upgrade_cost|valid_imps|religion
WARRIOR = "0|0|Warrior|UNIT_WARRIOR|10,24|2.0/2.0|100/100|20|0|0||0|0|||"
BUILDER = (
    "1|1|Builder|UNIT_BUILDER|12,22|2.0/2.0|100/100|0|0|3||0|0|||"
    "IMPROVEMENT_FARM;IMPROVEMENT_MINE|"
)

# 30 pipe-separated city fields: id|name|x,y|pop|food|prod|gold|sci|cul|faith|
#   housing|amenities|turns_grow|building|prod_turns|defense|gar_hp|wall_hp|
#   attack_targets|pillaged_districts|districts|loyalty|loyalty_max|loyalty_pt|
#   turns_flip|food_surplus|food_stored|growth_threshold|pillaged_buildings|garrison
CITY_LINE = (
    "0|Delhi|10,24|4|8.0|5.0|3.0|2.0|1.5|0.0|"
    "6.0|3|12|BUILDING_GRANARY|5|"
    "15|200/200|0/0|"
    "||DISTRICT_CITY_CENTER;DISTRICT_CAMPUS|"
    "100.0|100.0|5.0|0|3.5|20.0|36||Warrior"
)

# Map fields: x,y|terrain|feature|resource|hills|river|coastal|improvement|owner|
#   units|visibility|fresh_water|yields|district|owner_name|own_units|route|move_cost
PLAINS_TILE = (
    "10,24|TERRAIN_PLAINS|none|none|0|1|0|none|-1|none|visible|1|"
    "2,1,0,0,0,0|none||||-1|1"
)
HILLS_WITH_MINE = (
    "12,22|TERRAIN_PLAINS|none|RESOURCE_IRON:RESOURCECLASS_STRATEGIC|1|0|0|"
    "IMPROVEMENT_MINE|0|none|visible|0|1,3,0,0,0,0|none|India|none|-1|2"
)


# ---------------------------------------------------------------------------
# parse_gameover_response
# ---------------------------------------------------------------------------


def test_gameover_active_game_returns_none():
    assert parse_gameover_response(["GAME_ACTIVE"]) is None


def test_gameover_empty_lines_return_none():
    assert parse_gameover_response([]) is None


def test_gameover_parses_a_victory():
    result = parse_gameover_response(["GAME_OVER|VICTORY|Gandhi|SCIENCE|alive|Gandhi"])
    assert result is not None
    assert result.is_game_over is True
    assert result.is_defeat is False
    assert result.winner_name == "Gandhi"
    assert result.victory_type == "SCIENCE"
    assert result.player_alive is True
    assert result.winner_leader == "Gandhi"


def test_gameover_parses_a_defeat():
    result = parse_gameover_response(
        ["GAME_OVER|DEFEAT|Gilgamesh|DOMINATION|dead|Gilgamesh"]
    )
    assert result is not None
    assert result.is_defeat is True
    assert result.victory_type == "DOMINATION"
    assert result.player_alive is False


def test_gameover_defaults_missing_optional_fields():
    result = parse_gameover_response(["GAME_OVER|VICTORY"])
    assert result is not None
    assert result.winner_name == "Unknown"
    assert result.victory_type == "Unknown"


# ---------------------------------------------------------------------------
# parse_overview_response
# ---------------------------------------------------------------------------


def test_overview_parses_basic_fields():
    result = parse_overview_response([OVERVIEW_LINE])
    assert result.turn == 42
    assert result.player_id == 0
    assert result.civ_name == "CIVILIZATION_INDIA"
    assert result.leader_name == "Gandhi"
    assert result.gold == 500.0
    assert result.gold_per_turn == 10.5
    assert result.science_yield == 25.0
    assert result.culture_yield == 18.0
    assert result.faith == 12.0
    assert result.current_research == "TECH_POTTERY"
    assert result.current_civic == "CIVIC_CODE_OF_LAWS"
    assert result.num_cities == 3
    assert result.num_units == 5
    assert result.score == 120


def test_overview_parses_rankings():
    result = parse_overview_response(
        [OVERVIEW_LINE, "RANK|0|India|120", "RANK|1|Sumeria|95"]
    )
    assert result.rankings is not None
    assert len(result.rankings) == 2
    assert result.rankings[0].civ_name == "India"
    assert result.rankings[1].score == 95


def test_overview_parses_era_info():
    result = parse_overview_response([OVERVIEW_LINE, "ERA|Classical|15|12|24"])
    assert result.era_name == "Classical"
    assert result.era_score == 15
    assert result.era_dark_threshold == 12
    assert result.era_golden_threshold == 24


def test_overview_parses_exploration():
    result = parse_overview_response([OVERVIEW_LINE, "EXPLORE|200|1000"])
    assert result.explored_land == 200
    assert result.total_land == 1000


def test_overview_empty_response_raises():
    with pytest.raises(ValueError, match="Empty overview response"):
        parse_overview_response([])


def test_overview_too_few_fields_raises():
    with pytest.raises(ValueError, match="expected >=14"):
        parse_overview_response(["1|2|3"])


# ---------------------------------------------------------------------------
# parse_units_response
# ---------------------------------------------------------------------------


def test_units_parses_a_warrior():
    units = parse_units_response([WARRIOR])
    assert len(units) == 1
    u = units[0]
    assert u.unit_id == 0
    assert u.name == "Warrior"
    assert u.unit_type == "UNIT_WARRIOR"
    assert u.x == 10
    assert u.y == 24
    assert u.moves_remaining == 2.0
    assert u.health == 100
    assert u.combat_strength == 20
    assert u.ranged_strength == 0
    assert u.build_charges == 0


def test_units_parses_builder_improvements():
    u = parse_units_response([BUILDER])[0]
    assert u.build_charges == 3
    assert "IMPROVEMENT_FARM" in u.valid_improvements
    assert "IMPROVEMENT_MINE" in u.valid_improvements


def test_units_parses_multiple_units():
    assert len(parse_units_response([WARRIOR, BUILDER])) == 2


def test_units_skips_short_lines():
    assert parse_units_response(["too|few|fields"]) == []


def test_units_parses_attack_targets():
    line = "2|2|Archer|UNIT_ARCHER|5,5|2.0/2.0|100/100|25|25|0|14,6;15,7|0|0|||"
    assert parse_units_response([line])[0].targets == ["14,6", "15,7"]


# ---------------------------------------------------------------------------
# parse_combat_estimate
# ---------------------------------------------------------------------------


def test_combat_parses_a_melee_exchange():
    # ESTIMATE|att_type|def_type|eff_att_cs|eff_def_cs|is_ranged|modifiers|my_hp|enemy_hp
    line = "ESTIMATE|UNIT_WARRIOR|UNIT_WARRIOR|20|20|0|Flanking +2;Fortified -4|100|100"
    result = parse_combat_estimate([line], att_cs=20, def_cs=20)
    assert result is not None
    assert result.attacker_type == "UNIT_WARRIOR"
    assert result.defender_type == "UNIT_WARRIOR"
    assert result.attacker_cs == 20
    assert result.defender_cs == 20
    assert result.is_ranged is False
    assert "Flanking +2" in result.modifiers
    assert "Fortified -4" in result.modifiers
    # Equal CS: damage is the base 24 for both sides
    assert result.est_damage_to_defender == 24
    assert result.est_damage_to_attacker == 24


def test_combat_ranged_attacker_takes_no_counter_damage():
    result = parse_combat_estimate(
        ["ESTIMATE|UNIT_ARCHER|UNIT_WARRIOR|25|20|1||100|100"], att_cs=25, def_cs=20
    )
    assert result is not None
    assert result.is_ranged is True
    assert result.est_damage_to_attacker == 0
    assert result.est_damage_to_defender > 24  # attacker is stronger


def test_combat_missing_estimate_line_returns_none():
    assert parse_combat_estimate(["some other line"], att_cs=20, def_cs=20) is None


# ---------------------------------------------------------------------------
# parse_threat_scan_response
# ---------------------------------------------------------------------------


def test_threats_parse_a_standard_threat():
    line = (
        "THREAT|63|Barbarian|UNIT_WARRIOR|15,30|100/100|CS:20|RS:0|dist:3|cs:0|uid:42"
    )
    threats = parse_threat_scan_response([line])
    assert len(threats) == 1
    t = threats[0]
    assert t.owner_id == 63
    assert t.owner_name == "Barbarian"
    assert t.unit_type == "UNIT_WARRIOR"
    assert t.x == 15
    assert t.y == 30
    assert t.hp == 100
    assert t.combat_strength == 20
    assert t.distance == 3
    assert t.unit_id == 42


def test_threats_flag_city_states():
    line = "THREAT|10|Zanzibar|UNIT_ARCHER|8,12|80/100|CS:25|RS:25|dist:2|cs:1|uid:5"
    assert parse_threat_scan_response([line])[0].is_city_state is True


def test_threats_skip_unrelated_lines():
    assert parse_threat_scan_response(["SOME_OTHER_LINE", "ALSO_NOT_THREAT"]) == []


def test_threats_parse_the_legacy_format():
    """Older format carried no owner_id/owner_name."""
    threats = parse_threat_scan_response(
        ["THREAT|UNIT_WARRIOR|15,30|100/100|CS:20|RS:0|dist:3"]
    )
    assert len(threats) == 1
    assert threats[0].unit_type == "UNIT_WARRIOR"
    assert threats[0].x == 15


# ---------------------------------------------------------------------------
# parse_cities_response
# ---------------------------------------------------------------------------


def test_cities_parse_basic_fields():
    cities, _ = parse_cities_response([CITY_LINE])
    assert len(cities) == 1
    c = cities[0]
    assert c.city_id == 0
    assert c.name == "Delhi"
    assert c.x == 10
    assert c.y == 24
    assert c.population == 4
    assert c.food == 8.0
    assert c.production == 5.0
    assert c.currently_building == "BUILDING_GRANARY"
    assert c.production_turns_left == 5


def test_cities_parse_districts():
    cities, _ = parse_cities_response([CITY_LINE])
    assert "DISTRICT_CITY_CENTER" in cities[0].districts
    assert "DISTRICT_CAMPUS" in cities[0].districts


def test_cities_parse_inter_city_distances():
    cities, distances = parse_cities_response([CITY_LINE, "DIST|Delhi|Agra|8"])
    assert len(cities) == 1
    assert len(distances) == 1
    assert "8 tiles" in distances[0]


def test_cities_skip_short_lines():
    cities, _ = parse_cities_response(["too|short"])
    assert cities == []


# ---------------------------------------------------------------------------
# parse_map_response
# ---------------------------------------------------------------------------


def test_map_parses_a_basic_tile():
    tiles = parse_map_response([PLAINS_TILE])
    assert len(tiles) == 1
    t = tiles[0]
    assert t.x == 10
    assert t.y == 24
    assert t.terrain == "TERRAIN_PLAINS"
    assert t.feature is None
    assert t.resource is None
    assert t.is_hills is False
    assert t.is_river is True
    assert t.owner_id == -1
    assert t.visibility == "visible"
    assert t.is_fresh_water is True


def test_map_parses_resource_and_class():
    t = parse_map_response([HILLS_WITH_MINE])[0]
    assert t.resource == "RESOURCE_IRON"
    assert t.resource_class == "strategic"
    assert t.is_hills is True
    assert t.improvement == "IMPROVEMENT_MINE"
    assert t.owner_name == "India"
    assert t.movement_cost == 2


def test_map_parses_a_pillaged_improvement():
    line = (
        "5,5|TERRAIN_GRASSLAND|none|none|0|0|0|IMPROVEMENT_FARM:PILLAGED|0|none|"
        "visible|0|0,0,0,0,0,0|none||||-1|1"
    )
    t = parse_map_response([line])[0]
    assert t.improvement == "IMPROVEMENT_FARM"
    assert t.is_pillaged is True


def test_map_parses_yields():
    assert parse_map_response([PLAINS_TILE])[0].yields == (2, 1, 0, 0, 0, 0)


def test_map_skips_short_lines():
    assert parse_map_response(["too|few|fields"]) == []


# ---------------------------------------------------------------------------
# parse_end_turn_blocking
# ---------------------------------------------------------------------------


def test_blocking_none_returns_empty():
    assert parse_end_turn_blocking(["NONE"]) == []


def test_blocking_empty_lines_return_empty():
    assert parse_end_turn_blocking([]) == []


def test_blocking_parses_a_single_blocker():
    blockers = parse_end_turn_blocking(["BLOCKING|UNIT_NEEDS_ORDERS|Warrior at 10,24"])
    assert blockers == [("UNIT_NEEDS_ORDERS", "Warrior at 10,24")]


def test_blocking_parses_multiple_blockers():
    blockers = parse_end_turn_blocking(
        [
            "BLOCKING|UNIT_NEEDS_ORDERS|Warrior at 10,24",
            "BLOCKING|CHOOSE_PRODUCTION|Delhi needs production",
        ]
    )
    assert len(blockers) == 2
