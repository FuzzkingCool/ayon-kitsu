"""Batch /push relink retry (fullsync) for per-linked Concept folder collisions."""

from unittest.mock import MagicMock

import pytest
from ayon_api.exceptions import HTTPRequestError

from processor import task_relink


class _Resp:
    def __init__(self, ok: bool, data=None):
        self.data = data or {}

    def raise_for_status(self):
        return None


class _FolderCollision(Exception):
    """Mimics ayon_api HTTP body text used by is_folder_unique_violation."""


class _BadResp:
    """``post`` returns this; ``raise_for_status`` mimics HTTP error inside ``try``."""

    data: dict = {}

    def raise_for_status(self):
        raise _FolderCollision(
            "Folder with parent_id, name 'p, x' already exists. unique-violation"
        )


def test_push_batch_with_relink_retries_after_per_linked_relink(monkeypatch):
    """Second POST succeeds after try_relink updates folder_map."""
    calls: list[int] = []

    def fake_post(entrypoint, project_name=None, entities=None, **kwargs):
        calls.append(len(entities or []))
        if len(calls) == 1:
            return _BadResp()
        return _Resp(True, {"folders": {"link-1": "ayon-folder-1"}})

    monkeypatch.setattr(task_relink.ayon_api, "post", fake_post)

    touched = {"n": 0}

    def fake_relink(pn, ent, fm, concept_sync=None):
        touched["n"] += 1
        fm["link-1"] = "ayon-folder-1"
        return True

    monkeypatch.setattr(
        task_relink,
        "try_relink_per_linked_concept_folder_on_unique_violation",
        fake_relink,
    )
    monkeypatch.setattr(
        task_relink,
        "kitsu_folder_map_from_ayon_project",
        lambda pn: {"link-1": "ayon-folder-1"},
    )

    folder_map: dict = {}
    entities = [
        {"type": "Asset", "id": "a1"},
        {
            "type": "Concept",
            "id": "link-1",
            "__conceptSyncModel": "per_linked_entity",
            "kitsuSourceConceptIds": ["c1"],
        },
    ]
    r = task_relink.push_batch_with_relink(
        "addons/kitsu/x",
        "Proj",
        entities,
        folder_map,
    )
    r.raise_for_status()
    assert len(calls) == 2
    assert touched["n"] >= 1
    assert folder_map.get("link-1") == "ayon-folder-1"


def test_push_batch_with_relink_raises_when_not_folder_violation(monkeypatch):
    calls: list[int] = []

    class _Bad:
        data = {}

        def raise_for_status(self):
            raise RuntimeError("unrelated")

    def fake_post(entrypoint, project_name=None, entities=None, **kwargs):
        calls.append(1)
        return _Bad()

    monkeypatch.setattr(task_relink.ayon_api, "post", fake_post)

    with pytest.raises(RuntimeError, match="unrelated"):
        task_relink.push_batch_with_relink(
            "addons/kitsu/x",
            "Proj",
            [{"type": "Concept", "id": "x"}],
            {},
        )


def test_push_batch_with_relink_retries_single_link_legacy(monkeypatch):
    """Batch relink runs when ``concept_sync`` is per-linked and row has one link."""
    calls: list[int] = []

    def fake_post(entrypoint, project_name=None, entities=None, **kwargs):
        calls.append(len(entities or []))
        if len(calls) == 1:
            return _BadResp()
        return _Resp(True, {"folders": {"link-1": "ayon-folder-1"}})

    monkeypatch.setattr(task_relink.ayon_api, "post", fake_post)

    touched = {"n": 0}

    def fake_relink(pn, ent, fm, concept_sync=None):
        touched["n"] += 1
        fm["link-1"] = "ayon-folder-1"
        return True

    monkeypatch.setattr(
        task_relink,
        "try_relink_per_linked_concept_folder_on_unique_violation",
        fake_relink,
    )
    monkeypatch.setattr(
        task_relink,
        "kitsu_folder_map_from_ayon_project",
        lambda pn: {"link-1": "ayon-folder-1"},
    )

    folder_map: dict = {}
    concept_sync = {"concept_entity_model": "per_linked_entity"}
    entities = [
        {"type": "Asset", "id": "a1"},
        {
            "type": "Concept",
            "id": "concept-row",
            "entity_concept_links": ["link-1"],
            "name": "X",
        },
    ]
    r = task_relink.push_batch_with_relink(
        "addons/kitsu/x",
        "Proj",
        entities,
        folder_map,
        concept_sync=concept_sync,
    )
    r.raise_for_status()
    assert len(calls) == 2
    assert touched["n"] >= 1


def test_list_active_folders_retries_on_502_then_succeeds(monkeypatch):
    folder = {"id": "f1", "data": {"kitsuId": "k1"}}
    resp502 = MagicMock()
    resp502.status_code = 502
    err502 = HTTPRequestError("502", response=resp502)
    seq = [err502, [folder]]

    def fake_get_folders(_pn, active=True):
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return iter(item)

    monkeypatch.setattr(task_relink.ayon_api, "get_folders", fake_get_folders)
    monkeypatch.setattr(task_relink.time, "sleep", lambda *_a, **_k: None)
    out = task_relink._list_active_folders("Proj")
    assert out == [folder]
