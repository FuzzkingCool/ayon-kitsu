# -*- coding: utf-8 -*-
"""
Process kitsu.comment_update_request events: add Kitsu task comment with
uniqueSprites and bubble up uniqueSprites to the parent Kitsu entity (Assets only).
Runs in the processor service; uses thread-local Kitsu host from processor.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import gazu

from . import utils as processor_utils

if TYPE_CHECKING:
    from .processor import KitsuProcessor


def process_comment_update_request(
    processor: "KitsuProcessor", src_event: dict
) -> None:
    """Handle a kitsu.comment_update_request event.

    - Resolve Kitsu task and status (new_status from event, case-insensitive).
    - Render comment with template and add to Kitsu task.
    - Update parent entity (Asset only) data.uniqueSprites with the value.
    """
    processor_utils.set_kitsu_host(processor.kitsu_server_url)
    summary = src_event.get("summary") or {}
    payload = src_event.get("payload") or {}

    kitsu_task_id = summary.get("kitsu_task_id")
    unique_sprites = summary.get("unique_sprites")
    new_status = summary.get("new_status")
    product_name = summary.get("product_name", "Unknown")
    version = summary.get("version", 1)
    template_cfg = payload.get("template_cfg") or {}

    if not kitsu_task_id:
        logging.warning("[comment_update] Missing kitsu_task_id in event summary")
        return
    if unique_sprites is None:
        logging.debug("[comment_update] No unique_sprites in summary, skipping")
        return

    try:
        task = gazu.task.get_task(kitsu_task_id)
    except Exception as e:
        logging.error(f"[comment_update] Failed to get Kitsu task {kitsu_task_id}: {e}")
        return

    # Resolve Kitsu task status: event new_status is AYON status (often short name). Compare case-insensitively.
    status_short = str(new_status or "").strip().upper() if new_status else None
    note_status = None
    if status_short:
        try:
            note_status = gazu.task.get_task_status_by_short_name(status_short)
        except Exception:
            pass
        if not note_status:
            try:
                note_status = gazu.task.get_task_status_by_name(str(new_status or "").strip())
            except Exception:
                pass
    if not note_status:
        try:
            note_status = gazu.task.get_task_status_by_short_name("WIP")
        except Exception:
            pass
    if not note_status:
        logging.warning("[comment_update] Could not resolve Kitsu task status, skipping comment")
        return

    data_map: dict[str, Any] = {
        "version": version,
        "family": "review",
        "name": product_name,
    }
    if str(unique_sprites).strip() not in ("", "0"):
        data_map["uniqueSprites"] = str(unique_sprites)
    comment_text = processor_utils.render_kitsu_comment(template_cfg, data_map)
    if not comment_text:
        logging.warning("[comment_update] Empty comment text, skipping")
        return

    try:
        gazu.task.add_comment(task, note_status, comment=comment_text)
        logging.info(
            f"[comment_update] Added comment to Kitsu task {kitsu_task_id} "
            f"({product_name}) with uniqueSprites={unique_sprites}"
        )
    except Exception as e:
        logging.error(f"[comment_update] Failed to add comment to task {kitsu_task_id}: {e}")
        return

    # Bubble up uniqueSprites to parent Kitsu entity (Assets only; Shots are skipped)
    entity_id = task.get("entity_id")
    if not entity_id:
        logging.debug("[comment_update] Task has no entity_id, skipping entity data update")
        return
    try:
        entity = gazu.entity.get_entity(entity_id)
    except Exception as e:
        logging.warning(f"[comment_update] Could not get parent entity {entity_id}: {e}")
        return
    entity_type_id = entity.get("entity_type_id")
    if entity_type_id:
        entity_type = gazu.entity.get_entity_type(entity_type_id)
        type_name = (entity_type or {}).get("name", "")
    else:
        type_name = ""
    if type_name == "Shot":
        logging.debug("[comment_update] uniqueSprites is for Assets only, skipping Shot entity")
        return
    try:
        gazu.asset.update_asset_data(entity, {"uniqueSprites": str(unique_sprites)})
        logging.info(
            f"[comment_update] Updated parent entity {entity_id} data.uniqueSprites={unique_sprites}"
        )
    except Exception as e:
        logging.warning(f"[comment_update] Failed to update entity {entity_id} data: {e}")
