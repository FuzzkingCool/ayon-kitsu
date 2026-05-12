"""Kitsu pinned checklist rows → AYON child tasks (processor).

See studio addon settings: sync_settings.checklist_subtasks.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import ayon_api
import gazu
from nxtools import slugify

from . import utils as processor_utils
from .checklist_constants import (
    DEFAULT_CHECKLIST_DONE_STATUS_NAME,
    DEFAULT_CHECKLIST_WIP_STATUS_NAME,
)

if TYPE_CHECKING:
    from .processor import KitsuProcessor

log = logging.getLogger("checklist_subtask_sync")

DATA_KEY_CHECKLIST_ITEM = "kitsuChecklistItemKey"
DATA_KEY_PINNED_COMMENT = "kitsuPinnedCommentId"
DATA_KEY_CHECKLIST_INDEX = "kitsuChecklistIndex"
DATA_KEY_PARENT_KITSU_TASK = "kitsuParentKitsuTaskId"
DATA_KEY_PARENT_AYON_TASK = "kitsuParentAyonTaskId"


def _checklist_settings(processor: "KitsuProcessor") -> dict[str, Any]:
    return (
        (processor.settings.get("sync_settings") or {}).get("checklist_subtasks")
        or {}
    )


def checklist_subtasks_enabled(processor: "KitsuProcessor") -> bool:
    return bool(_checklist_settings(processor).get("enabled"))


def bulk_sync_pinned_checklists_after_fullsync_enabled(
    processor: "KitsuProcessor",
) -> bool:
    """True when bulk pinned-checklist pass should run after structural fullsync."""
    s = _checklist_settings(processor)
    return bool(s.get("enabled")) and bool(s.get("bulk_sync_after_fullsync"))


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def _task_type_name(task: dict[str, Any]) -> str:
    tt = task.get("taskType") or task.get("task_type")
    if isinstance(tt, dict):
        return str(tt.get("name") or "")
    return str(tt or "")


def _checklist_item_key(comment_id: str, index: int) -> str:
    return f"{comment_id}:{index}"


def _target_status(checked: bool, settings: dict[str, Any]) -> str:
    if checked:
        return str(
            settings.get("done_status_name") or DEFAULT_CHECKLIST_DONE_STATUS_NAME,
        )
    return str(
        settings.get("wip_status_name") or DEFAULT_CHECKLIST_WIP_STATUS_NAME,
    )


def checklist_checked_from_ayon_status(
    status: str | None, settings: dict[str, Any],
) -> bool:
    return _norm(status) == _norm(
        str(settings.get("done_status_name") or DEFAULT_CHECKLIST_DONE_STATUS_NAME),
    )


def _ayon_task_by_kitsu_id(project_name: str, kitsu_id: str) -> dict | None:
    """Resolve by ``kitsuId``; delegates to ``content_sync`` (pass cache when active)."""
    try:
        from . import content_sync

        return content_sync._ayon_task_by_kitsu_id(project_name, kitsu_id)
    except ImportError:
        for task in ayon_api.get_tasks(project_name):
            if (task.get("data") or {}).get("kitsuId") == kitsu_id:
                return task
        return None


def _iter_ayon_tasks(project_name: str):
    """Iterate project tasks; reuse pass-cache list when active, else this module's ``ayon_api``."""
    try:
        from . import content_sync

        c = content_sync._active_content_sync_ayon_cache(project_name)
        if c is not None:
            return c.iter_tasks()
    except ImportError:
        pass
    return iter(ayon_api.get_tasks(project_name))


def _tasks_for_checklist_comment(
    project_name: str,
    kitsu_comment_id: str,
    kitsu_parent_task_id: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for task in _iter_ayon_tasks(project_name):
        data = task.get("data") or {}
        if data.get(DATA_KEY_PINNED_COMMENT) != kitsu_comment_id:
            continue
        if data.get(DATA_KEY_PARENT_KITSU_TASK) != kitsu_parent_task_id:
            continue
        out.append(task)
    return out


def _task_for_checklist_item_key(
    project_name: str,
    folder_id: str,
    item_key: str,
) -> dict[str, Any] | None:
    """Resolve checklist child task by ``data.kitsuChecklistItemKey``.

    Folder-scoped. The ``data`` key is the canonical handle from a Kitsu
    checklist row to its AYON task; task ``name`` is now a counter-allocated
    slug and is no longer derivable from the item key.
    """
    for task in _iter_ayon_tasks(project_name):
        if task.get("folderId") != folder_id:
            continue
        if task.get("active") is False:
            continue
        data = task.get("data") or {}
        if data.get(DATA_KEY_CHECKLIST_ITEM) == item_key:
            return task
    return None


def _slug_name(text: str, index: int) -> str:
    base = slugify((text or "").strip(), separator="_") or f"checklist_{index}"
    safe = re.sub(r"[^a-zA-Z0-9_]+", "_", base).strip("_") or f"checklist_{index}"
    return safe[:120]


_CHECKLIST_NAME_MAX_LEN = 120
_CHECKLIST_NAME_COUNTER_LIMIT = 500
_CHECKLIST_CREATE_TOCTOU_RETRY = 5


def _allocate_checklist_child_task_name(
    project_name: str,
    folder_id: str,
    item_key: str,
    text: str,
    index: int,
) -> str:
    """Per-folder counter slug for a checklist child task name.

    Returns the existing name when a task already carries ``item_key`` under
    ``folder_id`` (re-sync is a no-op for the slug). Otherwise picks the first
    free ``<slug>``, ``<slug>_2``, ..., ``<slug>_499`` (mirrors the pattern in
    ``server/kitsu/utils.allocate_unique_concept_folder_name_label``).

    Caller wraps create_task in a TOCTOU retry: on a unique-violation re-call
    this allocator to skip the slot a sibling worker took between enumerate
    and create.
    """
    existing = _task_for_checklist_item_key(project_name, folder_id, item_key)
    if existing:
        existing_name = existing.get("name")
        if isinstance(existing_name, str) and existing_name:
            return existing_name

    base = _slug_name(text, index)
    suffix_budget = len(f"_{_CHECKLIST_NAME_COUNTER_LIMIT}")
    max_base_len = _CHECKLIST_NAME_MAX_LEN - suffix_budget
    if max_base_len < 8:
        max_base_len = 8
    base = base[:max_base_len].rstrip("_") or f"checklist_{index}"

    taken: set[str] = set()
    for task in _iter_ayon_tasks(project_name):
        if task.get("folderId") != folder_id:
            continue
        if task.get("active") is False:
            continue
        n = task.get("name")
        if isinstance(n, str):
            taken.add(n)

    for i in range(1, _CHECKLIST_NAME_COUNTER_LIMIT):
        candidate = base if i == 1 else f"{base}_{i}"
        if candidate not in taken:
            return candidate
    raise RuntimeError(
        f"Could not allocate a unique checklist child task slug for base "
        f"{base!r} under folder {folder_id!r} (project {project_name!r})"
    )


def _find_active_task_by_folder_and_name(
    project_name: str, folder_id: str, name: str,
) -> dict[str, Any] | None:
    """Single active task under ``folder_id`` with exact ``name`` (matches DB unique index)."""
    hits: list[dict[str, Any]] = []
    for task in _iter_ayon_tasks(project_name):
        if task.get("folderId") != folder_id or task.get("name") != name:
            continue
        if task.get("active") is False:
            continue
        hits.append(task)
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        log.warning(
            "checklist collision: multiple active tasks folder=%s name=%r count=%d",
            folder_id,
            name,
            len(hits),
        )
    return None


def _find_task_by_folder_name_and_parent(
    project_name: str,
    *,
    folder_id: str,
    name: str,
    parent_ayon_task_id: str,
) -> dict[str, Any] | None:
    hits: list[dict[str, Any]] = []
    for task in _iter_ayon_tasks(project_name):
        if task.get("folderId") != folder_id or task.get("name") != name:
            continue
        if str(task.get("parentId") or "") != str(parent_ayon_task_id):
            continue
        hits.append(task)
    if len(hits) == 1:
        return hits[0]
    return None


def _find_checklist_child_for_reuse(
    project_name: str,
    *,
    folder_id: str,
    name: str,
    parent_ayon_task_id: str,
    kitsu_comment_id: str,
    kitsu_parent_task_id: str,
) -> dict[str, Any] | None:
    t = _find_task_by_folder_name_and_parent(
        project_name,
        folder_id=folder_id,
        name=name,
        parent_ayon_task_id=parent_ayon_task_id,
    )
    if not t:
        return None
    d = t.get("data") or {}
    pin = d.get(DATA_KEY_PINNED_COMMENT)
    pk = d.get(DATA_KEY_PARENT_KITSU_TASK)
    if pin not in (None, "", kitsu_comment_id):
        return None
    if pk not in (None, "", kitsu_parent_task_id):
        return None
    return t


def _upsert_checklist_child_fields(
    project_name: str,
    existing: dict[str, Any],
    *,
    name: str,
    label: str,
    target_status: str,
    child_data: dict[str, Any],
) -> None:
    merged_data = {**(existing.get("data") or {}), **child_data}
    kwargs: dict[str, Any] = {}
    if _norm(existing.get("status")) != _norm(target_status):
        kwargs["status"] = target_status
    if existing.get("name") != name or existing.get("label") != label:
        kwargs["name"] = name
        kwargs["label"] = label
    if merged_data != (existing.get("data") or {}):
        kwargs["data"] = merged_data
    if kwargs:
        try:
            ayon_api.update_task(
                project_name, existing["id"], **kwargs,
            )
        except Exception as exc:
            log.warning(
                "update_task child %s: %s", existing["id"], exc,
            )


def _is_task_exists_collision(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        "already exists" in msg
        or "409" in msg
        or "unique" in msg
    )


def _checklist_key_safe_for_adopt(existing: dict[str, Any], our_key: str | None) -> bool:
    d = existing.get("data") or {}
    ck = d.get(DATA_KEY_CHECKLIST_ITEM)
    return ck in (None, "", our_key)


def _maybe_set_task_parent(
    project_name: str, task_id: str, parent_ayon_task_id: str,
) -> None:
    try:
        ayon_api.send_batch_operations(
            project_name,
            [
                {
                    "type": "update",
                    "entityType": "task",
                    "entityId": task_id,
                    "data": {"parentId": parent_ayon_task_id},
                }
            ],
            raise_on_fail=True,
        )
    except Exception as exc2:
        log.error(
            "checklist child %s: could not set parentId to %s: %s",
            task_id,
            parent_ayon_task_id,
            exc2,
        )


def _create_child_task_with_parent(
    project_name: str,
    *,
    name: str,
    label: str,
    task_type: str,
    folder_id: str,
    parent_ayon_task_id: str,
    status: str,
    data: dict[str, Any],
) -> str:
    """Create a task with ``parentId`` via batch operations (not exposed on create_task).

    Raises on a name-collision the in-place adoption path can't safely resolve;
    the caller is expected to re-allocate a fresh slug and retry.
    """
    from ayon_api.utils import create_entity_id

    our_key = data.get(DATA_KEY_CHECKLIST_ITEM)
    new_id = create_entity_id()
    payload: dict[str, Any] = {
        "id": new_id,
        "name": name,
        "label": label,
        "taskType": task_type,
        "folderId": folder_id,
        "parentId": parent_ayon_task_id,
        "status": status,
        "data": data,
    }
    op: dict[str, Any] = {
        "type": "create",
        "entityType": "task",
        "entityId": new_id,
        "data": payload,
    }
    try:
        ayon_api.send_batch_operations(project_name, [op], raise_on_fail=True)
        return new_id
    except Exception as exc:
        if _is_task_exists_collision(exc) and (
            hit := _find_task_by_folder_name_and_parent(
                project_name,
                folder_id=folder_id,
                name=name,
                parent_ayon_task_id=parent_ayon_task_id,
            )
        ):
            log.info(
                "Reused checklist child task %s (same folder+name+parent) "
                "for checklist key %r",
                hit["id"],
                our_key,
            )
            _upsert_checklist_child_fields(
                project_name,
                hit,
                name=name,
                label=label,
                target_status=status,
                child_data=data,
            )
            return str(hit["id"])
        if _is_task_exists_collision(exc) and (
            hit_ft := _find_active_task_by_folder_and_name(
                project_name, str(folder_id), name,
            )
        ):
            if _checklist_key_safe_for_adopt(hit_ft, our_key if isinstance(our_key, str) else None):
                log.info(
                    "Reused existing task %s for checklist key %r "
                    "(folder+name unique adopt)",
                    hit_ft["id"],
                    our_key,
                )
                _upsert_checklist_child_fields(
                    project_name,
                    hit_ft,
                    name=name,
                    label=label,
                    target_status=status,
                    child_data=data,
                )
                if str(hit_ft.get("parentId") or "") != str(parent_ayon_task_id):
                    _maybe_set_task_parent(
                        project_name, str(hit_ft["id"]), parent_ayon_task_id,
                    )
                return str(hit_ft["id"])
            log.error(
                "Refusing to adopt task %s: folder+name collision with different "
                "kitsuChecklistItemKey (existing=%r ours=%r). Caller will "
                "re-allocate a fresh slug.",
                hit_ft.get("id"),
                (hit_ft.get("data") or {}).get(DATA_KEY_CHECKLIST_ITEM),
                our_key,
            )
        log.warning(
            "create task with parentId failed (%s), retry without parent then update",
            exc,
        )
        try:
            tid = ayon_api.create_task(
                project_name,
                name=name,
                task_type=task_type,
                folder_id=folder_id,
                label=label,
                status=status,
                data=data,
            )
        except Exception as exc_ct:
            if _is_task_exists_collision(exc_ct) and (
                hit2 := _find_task_by_folder_name_and_parent(
                    project_name,
                    folder_id=folder_id,
                    name=name,
                    parent_ayon_task_id=parent_ayon_task_id,
                )
            ):
                log.info(
                    "Reused checklist child task %s after create_task collision "
                    "(same parent) key=%r",
                    hit2["id"],
                    our_key,
                )
                _upsert_checklist_child_fields(
                    project_name,
                    hit2,
                    name=name,
                    label=label,
                    target_status=status,
                    child_data=data,
                )
                return str(hit2["id"])
            if _is_task_exists_collision(exc_ct) and (
                hit_ft2 := _find_active_task_by_folder_and_name(
                    project_name, str(folder_id), name,
                )
            ):
                if _checklist_key_safe_for_adopt(
                    hit_ft2, our_key if isinstance(our_key, str) else None,
                ):
                    log.info(
                        "Reused existing task %s after create_task collision "
                        "(folder+name adopt) key=%r",
                        hit_ft2["id"],
                        our_key,
                    )
                    _upsert_checklist_child_fields(
                        project_name,
                        hit_ft2,
                        name=name,
                        label=label,
                        target_status=status,
                        child_data=data,
                    )
                    if str(hit_ft2.get("parentId") or "") != str(parent_ayon_task_id):
                        _maybe_set_task_parent(
                            project_name, str(hit_ft2["id"]), parent_ayon_task_id,
                        )
                    return str(hit_ft2["id"])
                log.error(
                    "Refusing to adopt task %s after create_task collision: "
                    "different kitsuChecklistItemKey (existing=%r ours=%r). "
                    "Caller will re-allocate a fresh slug.",
                    hit_ft2.get("id"),
                    (hit_ft2.get("data") or {}).get(DATA_KEY_CHECKLIST_ITEM),
                    our_key,
                )
            raise
        _maybe_set_task_parent(project_name, tid, parent_ayon_task_id)
        return tid


def delete_checklist_subtasks_for_comment(
    processor: "KitsuProcessor",
    project_name: str,
    kitsu_comment_id: str,
    kitsu_parent_task_id: str,
) -> None:
    settings = _checklist_settings(processor)
    if not settings.get("delete_tasks_on_comment_delete", True):
        return
    for task in _tasks_for_checklist_comment(
        project_name, kitsu_comment_id, kitsu_parent_task_id,
    ):
        try:
            ayon_api.delete_task(project_name, task["id"])
            log.info(
                "Deleted checklist child task %s for comment %s",
                task["id"], kitsu_comment_id[:8],
            )
        except Exception as exc:
            log.warning(
                "delete_task failed %s: %s", task.get("id"), exc,
            )


def _delete_orphans_over_index(
    project_name: str,
    kitsu_comment_id: str,
    kitsu_parent_task_id: str,
    keep_count: int,
) -> None:
    for task in _tasks_for_checklist_comment(
        project_name, kitsu_comment_id, kitsu_parent_task_id,
    ):
        idx = (task.get("data") or {}).get(DATA_KEY_CHECKLIST_INDEX)
        if isinstance(idx, int) and idx >= keep_count:
            try:
                ayon_api.delete_task(project_name, task["id"])
                log.info(
                    "Removed checklist child past index %s: %s",
                    keep_count, task["id"],
                )
            except Exception as exc:
                log.warning("delete orphan task %s: %s", task.get("id"), exc)


def sync_pinned_checklist_subtasks(
    processor: "KitsuProcessor",
    kitsu_task_id: str,
    kitsu_comment_id: str,
    kitsu_project_id: str,
) -> None:
    """Upsert AYON child tasks from a Kitsu comment checklist (processor thread)."""
    if not checklist_subtasks_enabled(processor):
        return

    project_name = processor.get_paired_ayon_project(kitsu_project_id)
    if not project_name:
        return

    processor_utils.set_kitsu_host(processor.kitsu_server_url)

    settings = _checklist_settings(processor)

    try:
        comment = gazu.task.get_comment(kitsu_comment_id)
    except Exception as exc:
        log.error("get_comment %s failed: %s", kitsu_comment_id, exc)
        return

    if not comment:
        return

    pinned = bool(comment.get("pinned"))
    checklist = comment.get("checklist") or []
    if not isinstance(checklist, list):
        checklist = []

    if not pinned:
        if settings.get("delete_tasks_when_unpinned"):
            delete_checklist_subtasks_for_comment(
                processor, project_name, kitsu_comment_id, kitsu_task_id,
            )
        return

    parent_ayon = _ayon_task_by_kitsu_id(project_name, kitsu_task_id)
    if not parent_ayon:
        log.debug(
            "No AYON parent task for kitsu task %s; skip checklist sync",
            kitsu_task_id,
        )
        return

    parent_id = parent_ayon["id"]
    folder_id = parent_ayon.get("folderId") or parent_ayon.get("folder_id")
    if not folder_id:
        log.warning("Parent task %s has no folderId", parent_id)
        return

    task_type = _task_type_name(parent_ayon)
    if not task_type:
        log.warning("Parent task %s has no task type", parent_id)
        return

    if not checklist:
        delete_checklist_subtasks_for_comment(
            processor, project_name, kitsu_comment_id, kitsu_task_id,
        )
        return

    kitsu_person: dict[str, Any] = {}
    raw_pid = comment.get("person_id")
    if isinstance(raw_pid, dict):
        raw_pid = raw_pid.get("id")
    if raw_pid:
        try:
            loaded = gazu.person.get_person(str(raw_pid))
            if isinstance(loaded, dict):
                kitsu_person = loaded
        except Exception as exc:
            log.debug("get_person for checklist sync %s: %s", raw_pid, exc)
    email_cache: dict[str, str] = {}

    def _checklist_ayon_mutations() -> None:
        _delete_orphans_over_index(
            project_name, kitsu_comment_id, kitsu_task_id, len(checklist),
        )
        by_key = {
            (t.get("data") or {}).get(DATA_KEY_CHECKLIST_ITEM): t
            for t in _tasks_for_checklist_comment(
                project_name, kitsu_comment_id, kitsu_task_id,
            )
        }
        for index, item in enumerate(checklist):
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip() or f"Item {index + 1}"
            checked = bool(item.get("checked"))
            target_status = _target_status(checked, settings)
            key = _checklist_item_key(kitsu_comment_id, index)

            child_data: dict[str, Any] = {
                DATA_KEY_CHECKLIST_ITEM: key,
                DATA_KEY_PINNED_COMMENT: kitsu_comment_id,
                DATA_KEY_CHECKLIST_INDEX: index,
                DATA_KEY_PARENT_KITSU_TASK: kitsu_task_id,
                DATA_KEY_PARENT_AYON_TASK: parent_id,
            }

            name = _allocate_checklist_child_task_name(
                project_name, str(folder_id), key, text, index,
            )
            existing = by_key.get(key)
            if not existing:
                existing = _find_checklist_child_for_reuse(
                    project_name,
                    folder_id=str(folder_id),
                    name=name,
                    parent_ayon_task_id=str(parent_id),
                    kitsu_comment_id=kitsu_comment_id,
                    kitsu_parent_task_id=kitsu_task_id,
                )

            if existing:
                _upsert_checklist_child_fields(
                    project_name,
                    existing,
                    name=name,
                    label=text[:500],
                    target_status=target_status,
                    child_data=child_data,
                )
                continue

            created = False
            last_exc: Exception | None = None
            for attempt in range(_CHECKLIST_CREATE_TOCTOU_RETRY):
                try:
                    _create_child_task_with_parent(
                        project_name,
                        name=name,
                        label=text[:500],
                        task_type=task_type,
                        folder_id=folder_id,
                        parent_ayon_task_id=parent_id,
                        status=target_status,
                        data=child_data,
                    )
                    created = True
                    log.info(
                        "Created checklist child for %s idx=%s name=%r",
                        kitsu_comment_id[:8], index, name,
                    )
                    break
                except Exception as exc:
                    last_exc = exc
                    if not _is_task_exists_collision(exc):
                        break
                    name = _allocate_checklist_child_task_name(
                        project_name, str(folder_id), key, text, index,
                    )
                    log.info(
                        "checklist child create TOCTOU comment=%s idx=%s; "
                        "re-allocated to %r (attempt %d)",
                        kitsu_comment_id[:8], index, name, attempt + 2,
                    )
            if not created:
                log.error(
                    "create checklist child failed comment=%s idx=%s: %s",
                    kitsu_comment_id[:8], index, last_exc,
                )

    from . import content_sync as _content_sync_mod

    _content_sync_mod._run_ayon_as_kitsu_person_when_service(
        processor,
        project_name,
        kitsu_person,
        email_cache,
        _checklist_ayon_mutations,
        acl_log_label="Checklist subtasks",
    )


def maybe_sync_checklist_subtasks_from_kitsu_comment(
    processor: "KitsuProcessor",
    kitsu_task_id: str,
    kitsu_comment_id: str,
    kitsu_project_id: str,
) -> None:
    """Entry point from comment:new / comment:update / fullsync."""
    try:
        sync_pinned_checklist_subtasks(
            processor, kitsu_task_id, kitsu_comment_id, kitsu_project_id,
        )
    except Exception:
        log.exception(
            "checklist subtask sync failed task=%s comment=%s",
            kitsu_task_id, kitsu_comment_id[:8],
        )
