"""Processor-side concept title normalization (must match server concept_utils)."""

import gazu

from processor import concept_naming
from processor import utils as processor_utils


def test_apply_concept_title_keeps_linked_entity_names_on_payload():
    ent = {
        "id": "uuid",
        "name": "ignored",
        "linked_entity_names": ["LinkedOnly"],
        "type": "Concept",
    }
    out = concept_naming.apply_concept_title_for_ayon_push(ent)
    assert out["name"] == "LinkedOnly"
    assert out["linked_entity_names"] == ["LinkedOnly"]


def test_apply_concept_title_moves_code_to_name_when_name_is_file_uuid():
    name = "temp1.png-5f893bd8-69eb-49d7-9093-fb4cc8327894"
    ent = {
        "id": "uuid",
        "name": name,
        "code": "VisibleTitle",
        "type": "Concept",
    }
    out = concept_naming.apply_concept_title_for_ayon_push(ent)
    assert out["name"] == "VisibleTitle"
    assert out["code"] == "VisibleTitle"


def test_all_concepts_fallback_normalizes_rows(monkeypatch):
    rows = [
        {
            "id": "a",
            "name": "x.png-8f2e6c8a-1b2a-3c4d-5e6f-708192a0b1c2",
            "code": "FromCode",
            "type": "Concept",
        }
    ]
    monkeypatch.setattr(
        processor_utils,
        "fetch_concepts_list_via_data_endpoint",
        lambda pid, parent=None: None,
    )
    monkeypatch.setattr(
        gazu.concept,
        "all_concepts_for_project",
        lambda project: list(rows),
    )
    monkeypatch.setattr(
        gazu.concept,
        "get_concept",
        lambda cid: dict(rows[0]) | {"id": cid},
    )
    out = processor_utils.all_concepts_for_project_official_list("proj-id-1")
    assert len(out) == 1
    assert out[0]["name"] == "FromCode"


def test_all_concepts_resolves_linked_entity_names_for_title(monkeypatch):
    rows = [
        {
            "id": "c1",
            "name": "x.png-8f2e6c8a-1b2a-3c4d-5e6f-708192a0b1c2",
            "code": "",
            "type": "Concept",
            "entity_concept_links": ["asset-1", "asset-2"],
        }
    ]
    monkeypatch.setattr(
        processor_utils,
        "fetch_concepts_list_via_data_endpoint",
        lambda pid, parent=None: None,
    )
    monkeypatch.setattr(
        gazu.concept,
        "all_concepts_for_project",
        lambda project: list(rows),
    )
    monkeypatch.setattr(
        gazu.concept,
        "get_concept",
        lambda cid: dict(rows[0]) | {"id": cid},
    )

    def fake_get_entity(eid):
        if eid == "asset-1":
            return {"id": eid, "name": "Hero"}
        if eid == "asset-2":
            return {"id": eid, "name": "Villain"}
        return None

    monkeypatch.setattr(gazu.entity, "get_entity", fake_get_entity)
    out = processor_utils.all_concepts_for_project_official_list(
        "proj-id-1",
        concept_sync={"prefer_linked_asset_names": True},
    )
    assert len(out) == 1
    assert out[0]["name"] == "Hero, Villain"
    assert out[0]["linked_entity_names"] == ["Hero", "Villain"]


def test_all_concepts_skips_linked_resolution_when_disabled(monkeypatch):
    rows = [
        {
            "id": "c1",
            "name": "x.png-8f2e6c8a-1b2a-3c4d-5e6f-708192a0b1c2",
            "code": "EditorCode",
            "type": "Concept",
            "entity_concept_links": ["asset-1"],
        }
    ]
    monkeypatch.setattr(
        processor_utils,
        "fetch_concepts_list_via_data_endpoint",
        lambda pid, parent=None: None,
    )
    monkeypatch.setattr(
        gazu.concept,
        "all_concepts_for_project",
        lambda project: list(rows),
    )
    monkeypatch.setattr(
        gazu.concept,
        "get_concept",
        lambda cid: dict(rows[0]) | {"id": cid},
    )
    def _no_get_entity(eid):
        raise AssertionError("get_entity must not run")

    monkeypatch.setattr(gazu.entity, "get_entity", _no_get_entity)
    out = processor_utils.all_concepts_for_project_official_list(
        "proj-id-1",
        concept_sync={"prefer_linked_asset_names": False},
    )
    assert len(out) == 1
    assert out[0]["name"] == "EditorCode"
