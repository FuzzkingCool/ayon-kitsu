"""Checklist subtask sync: reuse existing tasks by folder+name, 409 recovery."""

from __future__ import annotations

import sys
import types

import pytest

try:
    import gazu as _gazu_probe  # noqa: F401
except ImportError:
    _stub = types.ModuleType("gazu")
    _stub.entity = types.SimpleNamespace(get_entity=lambda _eid: None)
    _stub.task = types.SimpleNamespace(get_comment=lambda _cid: None)
    _stub.playlist = types.SimpleNamespace(
        all_shots_for_playlist=lambda *_a, **_k: [],
        get_playlist=lambda _pid: {"id": _pid, "name": "stub", "shots": []},
    )
    _stub.set_host = lambda *_a, **_k: None
    sys.modules["gazu"] = _stub

from processor import checklist_subtask_sync as m


def test_find_checklist_child_for_reuse_accepts_orphan_same_slug(monkeypatch):
    orphan = {
        "id": "task-orphan",
        "folderId": "fld-1",
        "name": "my_item",
        "parentId": "parent-1",
        "data": {},
    }
    import ayon_api as ayon_mod

    monkeypatch.setattr(ayon_mod, "get_tasks", lambda _pn: [orphan], raising=False)
    got = m._find_checklist_child_for_reuse(
        "Proj",
        folder_id="fld-1",
        name="my_item",
        parent_ayon_task_id="parent-1",
        kitsu_comment_id="cmt-99",
        kitsu_parent_task_id="kt-1",
    )
    assert got == orphan


def test_find_checklist_child_for_reuse_rejects_other_comment(monkeypatch):
    row = {
        "id": "t1",
        "folderId": "fld-1",
        "name": "my_item",
        "parentId": "parent-1",
        "data": {
            m.DATA_KEY_PINNED_COMMENT: "other-comment",
            m.DATA_KEY_PARENT_KITSU_TASK: "kt-1",
        },
    }
    import ayon_api as ayon_mod

    monkeypatch.setattr(ayon_mod, "get_tasks", lambda _pn: [row], raising=False)
    assert (
        m._find_checklist_child_for_reuse(
            "Proj",
            folder_id="fld-1",
            name="my_item",
            parent_ayon_task_id="parent-1",
            kitsu_comment_id="cmt-99",
            kitsu_parent_task_id="kt-1",
        )
        is None
    )


def test_create_child_recover_on_batch_collision(monkeypatch):
    import ayon_api as ayon_mod

    calls: list[str] = []

    def boom_batch(*_a, **_k):
        calls.append("batch")
        raise RuntimeError("Folder task already exists (unique)")

    existing = {
        "id": "existing-1",
        "folderId": "f1",
        "name": "slug",
        "parentId": "p1",
        "status": "todo",
        "label": "old",
        "data": {},
    }

    monkeypatch.setattr(ayon_mod, "send_batch_operations", boom_batch, raising=False)
    monkeypatch.setattr(ayon_mod, "get_tasks", lambda _pn: [existing], raising=False)
    updates: list[tuple] = []

    def capture_update(pn, tid, **kwargs):
        updates.append((pn, tid, kwargs))

    monkeypatch.setattr(ayon_mod, "update_task", capture_update, raising=False)

    tid = m._create_child_task_with_parent(
        "MyProject",
        name="slug",
        label="Label",
        task_type="VizDev",
        folder_id="f1",
        parent_ayon_task_id="p1",
        status="done",
        data={m.DATA_KEY_CHECKLIST_ITEM: "k:0"},
    )
    assert tid == "existing-1"
    assert calls == ["batch"]
    assert len(updates) == 1
    assert updates[0][1] == "existing-1"
    assert updates[0][2].get("status") == "done"
