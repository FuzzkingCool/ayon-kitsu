# -*- coding: utf-8 -*-
"""Dispatch Kitsu checklist PATCH jobs when AYON checklist-linked tasks change status."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from ayon_server.events import dispatch_event


def _extract_new_old_status(event: Any, project_name: str, task_id: str) -> tuple[Optional[str], Optional[str]]:
    """Best-effort status resolution (mirrors version_status_handler patterns)."""
    from .version_status_handler import get_task_entity

    new_status_raw = event.payload.get("newValue") or event.payload.get("status")
    new_status = (
        str(new_status_raw).strip() if new_status_raw is not None else None
    )
    old_status = event.payload.get("oldValue")

    if not new_status and event.topic in (
        "entity.task.updated",
        "entity.task.data_changed",
    ):
        new_status = (
            event.summary.get("status")
            or event.payload.get("status")
            or event.summary.get("newValue")
            or event.payload.get("newValue")
        )
        updated_fields = event.summary.get("updatedFields", []) or []
        if not new_status and task_id and "status" in updated_fields:
            task_entity = get_task_entity(project_name, task_id)
            if task_entity:
                new_status = task_entity.get("status")
        if not new_status:
            return None, old_status

    return new_status, old_status


async def try_dispatch_checklist_kitsu_update(addon, event) -> bool:
    """If event targets a checklist child task, dispatch processor job and return True."""
    bundle_name = os.getenv("AYON_BUNDLE_NAME", "Unknown")
    from .version_status_handler import get_task_entity

    project_name = event.project
    task_id = (
        event.summary.get("entityId")
        or event.summary.get("id")
        or event.summary.get("taskId")
    )
    if not project_name or not task_id:
        return False

    task_entity = get_task_entity(project_name, task_id)
    if not task_entity:
        return False

    data = task_entity.get("data") or {}
    if not data.get("kitsuChecklistItemKey"):
        return False

    studio = await addon.get_studio_settings()
    sync = getattr(studio, "sync_settings", None)
    cs = getattr(sync, "checklist_subtasks", None) if sync is not None else None
    if cs is None or not bool(getattr(cs, "enabled", False)):
        return False

    new_status, old_status = _extract_new_old_status(event, project_name, task_id)
    if not new_status:
        logging.debug(
            "[%s] [ayon-kitsu] checklist dispatch: no resolved status for task %s",
            bundle_name,
            task_id,
        )
        return False

    comment_id = data.get("kitsuPinnedCommentId")
    index = data.get("kitsuChecklistIndex")
    parent_kitsu = data.get("kitsuParentKitsuTaskId")
    if comment_id is None or index is None or parent_kitsu is None:
        logging.warning(
            "[%s] [ayon-kitsu] checklist task %s missing comment/index/parent data",
            bundle_name,
            task_id,
        )
        return False

    done_name = str(getattr(cs, "done_status_name", "") or "Done")
    wip_name = str(getattr(cs, "wip_status_name", "") or "In Progress")

    try:
        await dispatch_event(
            "kitsu.checklist_kitsu_update_request",
            description="Sync Kitsu pinned checklist from AYON task status",
            project=project_name,
            summary={
                "ayon_task_id": task_id,
                "kitsu_comment_id": str(comment_id),
                "checklist_index": int(index),
                "kitsu_parent_task_id": str(parent_kitsu),
                "new_status": new_status,
                "old_status": old_status,
                "done_status_name": done_name,
                "wip_status_name": wip_name,
            },
            payload={},
        )
        logging.info(
            "[%s] [ayon-kitsu] Dispatched checklist Kitsu update for task %s comment %s[%s]",
            bundle_name,
            task_id,
            str(comment_id)[:8],
            index,
        )
    except Exception as e:
        logging.error(
            "[%s] [ayon-kitsu] checklist dispatch failed: %s",
            bundle_name,
            e,
        )
        raise

    return True
