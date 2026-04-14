# -*- coding: utf-8 -*-
"""Process kitsu.checklist_kitsu_update_request: update Kitsu comment checklist from AYON."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import gazu

from . import utils as processor_utils
from .checklist_constants import (
    DEFAULT_CHECKLIST_DONE_STATUS_NAME,
    DEFAULT_CHECKLIST_WIP_STATUS_NAME,
)
from .checklist_subtask_sync import (
    _checklist_settings,
    checklist_checked_from_ayon_status,
)

if TYPE_CHECKING:
    from .processor import KitsuProcessor

log = logging.getLogger("checklist_kitsu_update")


def process_checklist_kitsu_update_request(
    processor: "KitsuProcessor", src_event: dict[str, Any],
) -> None:
    """Set one checklist row on a pinned Kitsu comment from AYON child task status."""
    processor_utils.set_kitsu_host(processor.kitsu_server_url)

    summary = src_event.get("summary") or {}
    comment_id = summary.get("kitsu_comment_id")
    index = summary.get("checklist_index")
    new_status = summary.get("new_status")

    if not comment_id or index is None or not new_status:
        log.warning("[checklist_kitsu_update] Missing summary fields: %s", summary)
        return

    settings = _checklist_settings(processor)
    if not settings.get("enabled"):
        log.debug("[checklist_kitsu_update] Feature disabled in processor settings")
        return

    done_name = summary.get("done_status_name") or settings.get(
        "done_status_name", DEFAULT_CHECKLIST_DONE_STATUS_NAME,
    )
    wip_name = summary.get("wip_status_name") or settings.get(
        "wip_status_name", DEFAULT_CHECKLIST_WIP_STATUS_NAME,
    )
    eff = {"done_status_name": done_name, "wip_status_name": wip_name}
    desired_checked = checklist_checked_from_ayon_status(str(new_status), eff)

    try:
        comment = gazu.task.get_comment(comment_id)
    except Exception as exc:
        log.error("[checklist_kitsu_update] get_comment %s: %s", comment_id, exc)
        return

    if not comment:
        return
    if not bool(comment.get("pinned")):
        log.debug("[checklist_kitsu_update] comment %s not pinned, skip", comment_id[:8])
        return

    checklist = comment.get("checklist") or []
    if not isinstance(checklist, list) or index < 0 or index >= len(checklist):
        log.warning(
            "[checklist_kitsu_update] Bad checklist index %s len=%s",
            index, len(checklist) if isinstance(checklist, list) else None,
        )
        return

    row = checklist[index]
    if not isinstance(row, dict):
        log.warning("[checklist_kitsu_update] checklist row %s not a dict", index)
        return

    current_checked = bool(row.get("checked"))
    if current_checked == desired_checked:
        log.debug(
            "[checklist_kitsu_update] already checked=%s for idx=%s, skip Kitsu PUT",
            desired_checked, index,
        )
        return

    new_checklist: list[dict[str, Any]] = []
    for i, item in enumerate(checklist):
        if isinstance(item, dict):
            d = dict(item)
        else:
            d = {"text": str(item), "checked": False}
        if i == index:
            d["checked"] = desired_checked
        new_checklist.append(d)

    updated = dict(comment)
    updated["checklist"] = new_checklist

    try:
        gazu.task.update_comment(updated)
        log.info(
            "[checklist_kitsu_update] comment=%s idx=%s checked=%s (AYON status=%s)",
            str(comment_id)[:8], index, desired_checked, new_status,
        )
    except Exception as exc:
        log.error(
            "[checklist_kitsu_update] update_comment failed: %s", exc,
        )
