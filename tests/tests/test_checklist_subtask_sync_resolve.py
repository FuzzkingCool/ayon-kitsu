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


def test_allocate_checklist_child_task_name_stays_under_max_length(monkeypatch):
    import ayon_api as ayon_mod

    monkeypatch.setattr(ayon_mod, "get_tasks", lambda _pn: [], raising=False)
    long_text = "word_" * 80
    n = m._allocate_checklist_child_task_name(
        "Proj", "fld-1", "cmt-x:0", long_text, 0,
    )
    assert len(n) <= 120
    assert "_" in n


def test_allocate_checklist_child_task_name_picks_free_slug(monkeypatch):
    import ayon_api as ayon_mod

    monkeypatch.setattr(ayon_mod, "get_tasks", lambda _pn: [], raising=False)
    n = m._allocate_checklist_child_task_name(
        "Proj", "fld-1", "cmt-x:0", "First Item", 0,
    )
    assert n == "first_item"


def test_allocate_assigns_counter_for_duplicate_text(monkeypatch):
    """Two checklist rows with the same text under the same folder get
    ``<slug>`` and ``<slug>_2`` respectively."""
    import ayon_api as ayon_mod

    tasks: list[dict] = []
    monkeypatch.setattr(ayon_mod, "get_tasks", lambda _pn: list(tasks), raising=False)

    n1 = m._allocate_checklist_child_task_name(
        "Proj", "fld-1", "cmt-x:0", "Same Text", 0,
    )
    assert n1 == "same_text"
    tasks.append({
        "id": "t1",
        "folderId": "fld-1",
        "name": n1,
        "active": True,
        "data": {m.DATA_KEY_CHECKLIST_ITEM: "cmt-x:0"},
    })

    n2 = m._allocate_checklist_child_task_name(
        "Proj", "fld-1", "cmt-x:1", "Same Text", 1,
    )
    assert n2 == "same_text_2"


def test_allocate_returns_existing_name_on_resync(monkeypatch):
    """Re-syncing the same checklist row reuses its previously allocated slug."""
    import ayon_api as ayon_mod

    existing = {
        "id": "t1",
        "folderId": "fld-1",
        "name": "my_item_3",
        "active": True,
        "data": {m.DATA_KEY_CHECKLIST_ITEM: "cmt-x:0"},
    }
    monkeypatch.setattr(ayon_mod, "get_tasks", lambda _pn: [existing], raising=False)

    n = m._allocate_checklist_child_task_name(
        "Proj", "fld-1", "cmt-x:0", "My Item", 0,
    )
    assert n == "my_item_3"


def test_allocate_skips_unrelated_taken_slot(monkeypatch):
    """An unrelated task occupying the first slug is skipped; allocator picks the next."""
    import ayon_api as ayon_mod

    occupant = {
        "id": "t-other",
        "folderId": "fld-1",
        "name": "review",
        "active": True,
        "data": {},
    }
    monkeypatch.setattr(ayon_mod, "get_tasks", lambda _pn: [occupant], raising=False)
    n = m._allocate_checklist_child_task_name(
        "Proj", "fld-1", "cmt-x:0", "Review", 0,
    )
    assert n == "review_2"


def test_allocate_ignores_other_folder_with_same_item_key(monkeypatch):
    """Item-key lookup is folder-scoped: same key in another folder doesn't short-circuit."""
    import ayon_api as ayon_mod

    other_folder = {
        "id": "t-other-folder",
        "folderId": "fld-OTHER",
        "name": "old_slug",
        "active": True,
        "data": {m.DATA_KEY_CHECKLIST_ITEM: "cmt-x:0"},
    }
    monkeypatch.setattr(
        ayon_mod, "get_tasks", lambda _pn: [other_folder], raising=False,
    )
    n = m._allocate_checklist_child_task_name(
        "Proj", "fld-1", "cmt-x:0", "Fresh Text", 0,
    )
    assert n == "fresh_text"


def test_allocate_ignores_inactive_tasks(monkeypatch):
    """Inactive tasks don't claim slugs in the counter scan."""
    import ayon_api as ayon_mod

    inactive = {
        "id": "t-inactive",
        "folderId": "fld-1",
        "name": "review",
        "active": False,
        "data": {},
    }
    monkeypatch.setattr(ayon_mod, "get_tasks", lambda _pn: [inactive], raising=False)
    n = m._allocate_checklist_child_task_name(
        "Proj", "fld-1", "cmt-x:0", "Review", 0,
    )
    assert n == "review"


def test_task_for_checklist_item_key_returns_match(monkeypatch):
    import ayon_api as ayon_mod

    rows = [
        {
            "id": "t-other",
            "folderId": "fld-1",
            "name": "noise",
            "active": True,
            "data": {m.DATA_KEY_CHECKLIST_ITEM: "cmt-y:0"},
        },
        {
            "id": "t-hit",
            "folderId": "fld-1",
            "name": "wanted_slug_2",
            "active": True,
            "data": {m.DATA_KEY_CHECKLIST_ITEM: "cmt-x:0"},
        },
    ]
    monkeypatch.setattr(ayon_mod, "get_tasks", lambda _pn: rows, raising=False)
    hit = m._task_for_checklist_item_key("Proj", "fld-1", "cmt-x:0")
    assert hit is not None
    assert hit["id"] == "t-hit"


def test_task_for_checklist_item_key_no_match(monkeypatch):
    import ayon_api as ayon_mod

    monkeypatch.setattr(ayon_mod, "get_tasks", lambda _pn: [], raising=False)
    assert m._task_for_checklist_item_key("Proj", "fld-1", "cmt-x:0") is None


def test_create_child_adopts_folder_name_different_parent_orphan_key(monkeypatch):
    import ayon_api as ayon_mod

    orphan = {
        "id": "t-orphan",
        "folderId": "f1",
        "name": "slug_x",
        "parentId": "p-other",
        "active": True,
        "taskType": "VizDev",
        "status": "todo",
        "label": "old",
        "data": {},
    }
    op_log: list[str] = []

    def send_batch(pn, ops, raise_on_fail=True, **_k):
        if ops and ops[0].get("type") == "create":
            op_log.append("create_fail")
            raise RuntimeError("409 unique violation task name")
        op_log.append("parent_set")
        return None

    monkeypatch.setattr(ayon_mod, "send_batch_operations", send_batch, raising=False)
    monkeypatch.setattr(ayon_mod, "get_tasks", lambda _pn: [orphan], raising=False)
    captured: list[tuple] = []

    def upd(pn, tid, **kwargs):
        captured.append((pn, tid, kwargs))

    monkeypatch.setattr(ayon_mod, "update_task", upd, raising=False)
    tid = m._create_child_task_with_parent(
        "P",
        name="slug_x",
        label="Label",
        task_type="VizDev",
        folder_id="f1",
        parent_ayon_task_id="p1",
        status="done",
        data={m.DATA_KEY_CHECKLIST_ITEM: "k:0"},
    )
    assert tid == "t-orphan"
    assert "create_fail" in op_log
    assert "parent_set" in op_log
    assert captured


def test_create_child_raises_on_key_conflict_so_caller_can_reallocate(monkeypatch):
    """A folder+name collision with a different ``kitsuChecklistItemKey`` must
    bubble up so the outer TOCTOU loop re-allocates a fresh slug."""
    import ayon_api as ayon_mod

    name = "shared_slug"
    row = {
        "id": "t1",
        "folderId": "f1",
        "name": name,
        "parentId": "p2",
        "active": True,
        "taskType": "VizDev",
        "data": {
            m.DATA_KEY_CHECKLIST_ITEM: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb:0",
        },
    }

    def boom_batch(*_a, **_k):
        raise RuntimeError("409 unique violation")

    monkeypatch.setattr(ayon_mod, "send_batch_operations", boom_batch, raising=False)
    monkeypatch.setattr(ayon_mod, "get_tasks", lambda _pn: [row], raising=False)

    def boom_create(*_a, **_k):
        raise RuntimeError("409 unique")

    monkeypatch.setattr(ayon_mod, "create_task", boom_create, raising=False)
    with pytest.raises(RuntimeError):
        m._create_child_task_with_parent(
            "P",
            name=name,
            label="L",
            task_type="VizDev",
            folder_id="f1",
            parent_ayon_task_id="p1",
            status="done",
            data={
                m.DATA_KEY_CHECKLIST_ITEM:
                    "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa:0",
            },
        )
