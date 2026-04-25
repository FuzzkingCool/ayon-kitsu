"""Unit tests for Concept folder display naming and VizDev surrogate id helpers."""

import importlib.util
from pathlib import Path

_mod_path = Path(__file__).resolve().parents[2] / "server" / "kitsu" / "concept_utils.py"
_spec = importlib.util.spec_from_file_location("_concept_utils_under_test", _mod_path)
assert _spec and _spec.loader
_cu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cu)
concept_folder_display_name = _cu.concept_folder_display_name
concept_vizdev_surrogate_kitsu_id = _cu.concept_vizdev_surrogate_kitsu_id


def test_concept_vizdev_surrogate_kitsu_id():
    cid = "62707498-bbd2-4fcf-a7be-4b061942086c"
    assert concept_vizdev_surrogate_kitsu_id(cid) == f"kitsu:concept:{cid}:vizdev"


def test_concept_folder_display_name_strips_filename_uuid():
    raw = "temp1.png-5f893bd8-69eb-49d7-9093-fb4cc8327894"
    assert concept_folder_display_name(raw, sanitize=True) == "temp1"


def test_concept_folder_display_name_passthrough_when_no_match():
    raw = "ART_PorkrindKingdomVizDev"
    assert concept_folder_display_name(raw, sanitize=True) == raw


def test_concept_folder_display_name_sanitize_off():
    raw = "temp1.png-5f893bd8-69eb-49d7-9093-fb4cc8327894"
    assert concept_folder_display_name(raw, sanitize=False) == raw
