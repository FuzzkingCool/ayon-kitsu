"""Tests for per-linked-entity concept expansion before POST /push."""

from __future__ import annotations

import sys
import types
import uuid

try:
    import gazu as _gazu_probe  # noqa: F401
except ImportError:
    _stub = types.ModuleType("gazu")
    _stub.entity = types.SimpleNamespace(get_entity=lambda _eid: None)
    _stub.set_host = lambda *_a, **_k: None
    sys.modules["gazu"] = _stub

from processor import concept_expand


def _pid() -> str:
    return str(uuid.uuid4())


def test_expand_legacy_mode_returns_same_rows():
    pid = _pid()
    rows = [
        {
            "type": "Concept",
            "id": "c1",
            "project_id": pid,
            "name": "A",
            "entity_concept_links": ["e1"],
        }
    ]
    out = concept_expand.expand_concept_entities_for_push(
        rows,
        concept_sync={"concept_entity_model": "per_kitsu_concept"},
    )
    assert len(out) == 1
    assert out[0]["id"] == "c1"


def test_expand_dedupes_two_concepts_same_link_same_parent(monkeypatch):
    pid = _pid()
    link = str(uuid.uuid4())

    def fake_get_entity(eid: str):
        if eid == link:
            return {"id": link, "name": "SharedAsset", "code": "SA", "entity_type_id": "t1"}
        return None

    monkeypatch.setattr(
        "processor.concept_expand.gazu.entity.get_entity",
        fake_get_entity,
    )

    c1, c2 = str(uuid.uuid4()), str(uuid.uuid4())
    rows = [
        {
            "type": "Concept",
            "id": c1,
            "project_id": pid,
            "parent_id": "chapter-1",
            "entity_concept_links": [link],
            "preview_file_id": "pf1",
        },
        {
            "type": "Concept",
            "id": c2,
            "project_id": pid,
            "parent_id": "chapter-1",
            "entity_concept_links": [link],
            "preview_file_id": "pf2",
        },
    ]
    out = concept_expand.expand_concept_entities_for_push(
        rows,
        concept_sync={"concept_entity_model": "per_linked_entity"},
    )
    linked_rows = [r for r in out if r.get("__conceptSyncModel") == "per_linked_entity"]
    assert len(linked_rows) == 1
    r0 = linked_rows[0]
    assert r0["id"] == link
    assert c1 in (r0.get("kitsuSourceConceptIds") or [])
    assert c2 in (r0.get("kitsuSourceConceptIds") or [])


def test_expand_unlinked_inserts_hub_and_sets_parent(monkeypatch):
    pid = _pid()
    hub = "kitsu:concepts:unlinked_hub"

    monkeypatch.setattr(
        "processor.concept_expand.gazu.entity.get_entity",
        lambda _eid: None,
    )

    cid = str(uuid.uuid4())
    rows = [
        {
            "type": "Concept",
            "id": cid,
            "project_id": pid,
            "parent_id": None,
            "name": "Orphan",
        }
    ]
    out = concept_expand.expand_concept_entities_for_push(
        rows,
        concept_sync={
            "concept_entity_model": "per_linked_entity",
            "unlinked_concepts_hub_kitsu_id": hub,
            "unlinked_concepts_folder_label": "Hubby",
        },
    )
    hubs = [r for r in out if r.get("__conceptSyncModel") == "unlinked_hub"]
    assert len(hubs) == 1
    assert hubs[0]["id"] == hub
    unlinked = [r for r in out if r.get("__conceptSyncModel") == "per_concept_unlinked"]
    assert len(unlinked) == 1
    assert unlinked[0]["parent_id"] == hub


def test_expand_same_link_different_parent_id_collapses_to_one_row(monkeypatch):
    """Kitsu can represent parent_id inconsistently; same asset must not fan out."""
    pid = _pid()
    link = str(uuid.uuid4())

    def fake_get_entity(eid: str):
        if eid == link:
            return {"id": link, "name": "OneAsset", "code": "OA", "entity_type_id": "t1"}
        return None

    monkeypatch.setattr(
        "processor.concept_expand.gazu.entity.get_entity",
        fake_get_entity,
    )
    c1, c2 = str(uuid.uuid4()), str(uuid.uuid4())
    chapter = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    rows = [
        {
            "type": "Concept",
            "id": c1,
            "project_id": pid,
            "parent_id": None,
            "entity_concept_links": [link],
        },
        {
            "type": "Concept",
            "id": c2,
            "project_id": pid,
            "parent_id": chapter,
            "entity_concept_links": [link],
        },
    ]
    out = concept_expand.expand_concept_entities_for_push(
        rows,
        concept_sync={"concept_entity_model": "per_linked_entity"},
    )
    linked = [r for r in out if r.get("__conceptSyncModel") == "per_linked_entity"]
    assert len(linked) == 1
    assert linked[0]["parent_id"] == chapter
    assert c1 in linked[0]["kitsuSourceConceptIds"]
    assert c2 in linked[0]["kitsuSourceConceptIds"]


def test_expand_single_concept_delegates():
    pid = _pid()
    rows = concept_expand.expand_single_concept_entity_for_push(
        {"type": "Concept", "id": "x", "project_id": pid},
        concept_sync={"concept_entity_model": "per_kitsu_concept"},
    )
    assert len(rows) == 1


def test_normalize_concept_sync_camel_case_enables_per_linked_expand(monkeypatch):
    """Processor JSON may send conceptEntityModel; normalize before expand."""
    monkeypatch.delenv("KITSU_PROCESSOR_CONCEPT_ENTITY_MODEL", raising=False)
    pid = _pid()
    link = str(uuid.uuid4())

    def fake_get_entity(eid: str):
        if eid == link:
            return {"id": link, "name": "A", "code": "a", "entity_type_id": "t1"}
        return None

    monkeypatch.setattr(
        "processor.concept_expand.gazu.entity.get_entity",
        fake_get_entity,
    )
    raw_sync = {"conceptEntityModel": "per_linked_entity"}
    ns = concept_expand.normalize_concept_sync_dict(raw_sync)
    assert ns is not None
    assert ns.get("concept_entity_model") == "per_linked_entity"
    out = concept_expand.expand_concept_entities_for_push(
        [
            {
                "type": "Concept",
                "id": str(uuid.uuid4()),
                "project_id": pid,
                "entity_concept_links": [link],
            }
        ],
        concept_sync=ns,
    )
    linked = [r for r in out if r.get("__conceptSyncModel") == "per_linked_entity"]
    assert len(linked) == 1
    assert linked[0]["id"] == link


def test_expand_unlinked_project_anchor_emits_project_not_hub(monkeypatch):
    pid = _pid()
    monkeypatch.setattr(
        "processor.concept_expand.gazu.entity.get_entity",
        lambda _eid: None,
    )
    cid = str(uuid.uuid4())
    rows = [
        {
            "type": "Concept",
            "id": cid,
            "project_id": pid,
            "parent_id": None,
            "name": "Orphan",
        }
    ]
    out = concept_expand.expand_concept_entities_for_push(
        rows,
        concept_sync={
            "concept_entity_model": "per_linked_entity",
            "unlinked_concepts_anchor": "project",
            "unlinked_concepts_project_kitsu_id": "kitsu:test:proj",
            "unlinked_concepts_project_folder_label": "Project",
        },
    )
    assert not any(r.get("__conceptSyncModel") == "unlinked_hub" for r in out)
    assert not any(r.get("__conceptSyncModel") == "per_concept_unlinked" for r in out)
    anchors = [r for r in out if r.get("__conceptSyncModel") == "unlinked_project_anchor"]
    assert len(anchors) == 1
    assert anchors[0]["type"] == "Project"
    assert anchors[0]["id"] == "kitsu:test:proj"


def test_per_linked_effective_linked_id_expanded_row():
    ent = {
        "type": "Concept",
        "id": "link-uuid",
        "__conceptSyncModel": "per_linked_entity",
    }
    assert (
        concept_expand.per_linked_effective_linked_id_for_relink(
            ent,
            {"concept_entity_model": "per_linked_entity"},
        )
        == "link-uuid"
    )


def test_per_linked_effective_linked_id_single_link_legacy():
    ent = {
        "type": "Concept",
        "id": "concept-uuid",
        "entity_concept_links": ["link-uuid"],
        "name": "N",
    }
    sync = {"concept_entity_model": "per_linked_entity"}
    assert (
        concept_expand.per_linked_effective_linked_id_for_relink(ent, sync)
        == "link-uuid"
    )


def test_per_linked_effective_linked_id_none_without_setting():
    ent = {
        "type": "Concept",
        "id": "concept-uuid",
        "entity_concept_links": ["link-uuid"],
    }
    assert concept_expand.per_linked_effective_linked_id_for_relink(ent, None) is None


def test_should_attempt_relink_single_link_without_meta():
    ent = {
        "type": "Concept",
        "id": "c1",
        "entity_concept_links": ["L1"],
    }
    assert concept_expand.should_attempt_per_linked_concept_relink(
        ent,
        {"concept_entity_model": "per_linked_entity"},
    )


def test_should_not_attempt_relink_multi_link_without_meta():
    ent = {
        "type": "Concept",
        "id": "c1",
        "entity_concept_links": ["L1", "L2"],
    }
    assert not concept_expand.should_attempt_per_linked_concept_relink(
        ent,
        {"concept_entity_model": "per_linked_entity"},
    )


def test_normalize_concept_sync_env_override_fills_empty_json(monkeypatch):
    monkeypatch.setenv("KITSU_PROCESSOR_CONCEPT_ENTITY_MODEL", "per_linked_entity")
    ns = concept_expand.normalize_concept_sync_dict(
        {"concept_entity_model": "", "prefer_linked_asset_names": True},
    )
    assert ns is not None
    assert ns.get("concept_entity_model") == "per_linked_entity"
    assert ns.get("prefer_linked_asset_names") is True


def test_normalize_concept_sync_env_only_when_raw_none(monkeypatch):
    monkeypatch.setenv("KITSU_PROCESSOR_CONCEPT_ENTITY_MODEL", "per_linked_entity")
    ns = concept_expand.normalize_concept_sync_dict(None)
    assert ns == {"concept_entity_model": "per_linked_entity"}


def test_normalize_concept_sync_env_only_when_raw_empty_dict(monkeypatch):
    monkeypatch.setenv("KITSU_PROCESSOR_CONCEPT_ENTITY_MODEL", "per_linked_entity")
    ns = concept_expand.normalize_concept_sync_dict({})
    assert ns == {"concept_entity_model": "per_linked_entity"}


def test_normalize_concept_sync_strips_blank_model_no_env():
    ns = concept_expand.normalize_concept_sync_dict({"concept_entity_model": "   "})
    assert ns is None


def test_normalize_concept_sync_json_wins_without_env(monkeypatch):
    monkeypatch.delenv("KITSU_PROCESSOR_CONCEPT_ENTITY_MODEL", raising=False)
    ns = concept_expand.normalize_concept_sync_dict(
        {"concept_entity_model": "per_kitsu_concept"},
    )
    assert ns.get("concept_entity_model") == "per_kitsu_concept"
