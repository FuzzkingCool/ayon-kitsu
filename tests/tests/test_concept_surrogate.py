"""Unit tests for Concept folder display naming and VizDev surrogate id helpers."""

import importlib.util
from pathlib import Path

_mod_path = Path(__file__).resolve().parents[2] / "server" / "kitsu" / "concept_utils.py"
_spec = importlib.util.spec_from_file_location("_concept_utils_under_test", _mod_path)
assert _spec and _spec.loader
_cu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cu)
concept_folder_base_slug = _cu.concept_folder_base_slug
concept_folder_display_name = _cu.concept_folder_display_name
concept_primary_title_for_folder = _cu.concept_primary_title_for_folder
concept_vizdev_surrogate_kitsu_id = _cu.concept_vizdev_surrogate_kitsu_id
concept_vizdev_surrogate_for_linked_entity = _cu.concept_vizdev_surrogate_for_linked_entity


def test_concept_vizdev_surrogate_kitsu_id():
    cid = "62707498-bbd2-4fcf-a7be-4b061942086c"
    assert concept_vizdev_surrogate_kitsu_id(cid) == f"kitsu:concept:{cid}:vizdev"


def test_concept_vizdev_surrogate_for_linked_entity():
    lid = "11111111-1111-1111-1111-111111111111"
    assert concept_vizdev_surrogate_for_linked_entity(lid) == f"kitsu:link:{lid}:vizdev"


def test_concept_folder_display_name_strips_filename_uuid():
    raw = "temp1.png-5f893bd8-69eb-49d7-9093-fb4cc8327894"
    assert concept_folder_display_name(raw, sanitize=True) == "temp1"


def test_concept_folder_display_name_passthrough_when_no_match():
    raw = "ART_PorkrindKingdomVizDev"
    assert concept_folder_display_name(raw, sanitize=True) == raw


def test_concept_folder_display_name_sanitize_off():
    raw = "temp1.png-5f893bd8-69eb-49d7-9093-fb4cc8327894"
    assert concept_folder_display_name(raw, sanitize=False) == raw


def test_concept_folder_display_name_strips_leading_numeric_prefix():
    raw = "610197433-sheriff_concepts_12"
    assert concept_folder_display_name(raw, sanitize=True) == "sheriff_concepts_12"


def test_concept_primary_title_prefers_name_when_not_auto_import_style():
    assert (
        concept_primary_title_for_folder(
            {"name": "ART_Foo", "code": "610197433-bar"}
        )
        == "ART_Foo"
    )


def test_concept_primary_title_prefers_code_when_name_is_file_uuid_pattern():
    name = "temp1.png-5f893bd8-69eb-49d7-9093-fb4cc8327894"
    assert (
        concept_primary_title_for_folder(
            {"name": name, "code": "RenamedInKitsu"}
        )
        == "RenamedInKitsu"
    )


def test_concept_primary_title_prefers_code_when_name_has_leading_numeric_prefix():
    assert (
        concept_primary_title_for_folder(
            {"name": "610197433-sheriff_concepts_12", "code": "Sheriff Pack"}
        )
        == "Sheriff Pack"
    )


def test_concept_primary_title_falls_back_to_code():
    assert concept_primary_title_for_folder({"name": "", "code": "CODE1"}) == "CODE1"
    assert concept_primary_title_for_folder({"code": "X"}) == "X"


def test_concept_primary_title_prefers_linked_entity_names():
    long_name = "temp1.png-5f893bd8-69eb-49d7-9093-fb4cc8327894"
    assert (
        concept_primary_title_for_folder(
            {
                "name": long_name,
                "code": "CodeIgnoredWhenLinksPresent",
                "linked_entity_names": ["Hero", "Sidekick"],
            }
        )
        == "Hero, Sidekick"
    )


def test_concept_primary_title_linked_names_dedupe_and_strip():
    assert (
        concept_primary_title_for_folder(
            {"name": "n", "linked_entity_names": ["  A ", "A", "B", ""]}
        )
        == "A, B"
    )


def test_concept_folder_base_slug_from_label():
    assert concept_folder_base_slug("ART_Foo") == "art_foo"
    assert concept_folder_base_slug("  ") == "concept"
