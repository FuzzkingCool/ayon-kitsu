# -*- coding: utf-8 -*-
"""
Server-side event handler for task status changes.

This handler subscribes to entity.task.status_changed events, gathers all required data
from AYON (task, product, version, uniqueSprites, settings), and dispatches a
kitsu.comment_update_request event with complete payload to the processor service.

The processor service then handles the actual Kitsu API operations (gazu) without needing
access to AYON APIs or client modules.
"""

import logging
import os
import traceback
from typing import Any, Dict, Optional

from ayon_server.events import dispatch_event


async def handle_task_status_change(addon, event):
    """Handle version status changes to bubble up uniqueSprites to Kitsu comments.

    This function is called by the on_task_status_changed hook method in the addon class.
    Follows the canonical AYON pattern from ayon-example-addon.
    """
    bundle_name = os.getenv("AYON_BUNDLE_NAME", "Unknown")

    try:
        logging.info(
            f"[{bundle_name}] [ayon-kitsu] ===== TASK STATUS HANDLER CALLED ====="
        )
        logging.info(
            f"[{bundle_name}] [ayon-kitsu] Received event: {event.topic} for project: {event.project}"
        )
        if event.topic == "entity.task.data_changed":
            logging.debug(
                f"[{bundle_name}] [ayon-kitsu] data_changed (UI/core); will resolve status if needed"
            )
        logging.info(
            f"[{bundle_name}] [ayon-kitsu] Event summary keys: {list(event.summary.keys()) if hasattr(event, 'summary') else 'N/A'}"
        )
        logging.info(
            f"[{bundle_name}] [ayon-kitsu] Event payload keys: {list(event.payload.keys()) if hasattr(event, 'payload') else 'N/A'}"
        )
        logging.debug(
            f"[{bundle_name}] [ayon-kitsu] Full event summary: {event.summary}, payload: {event.payload}"
        )

        from .checklist_task_dispatch import try_dispatch_checklist_kitsu_update

        if await try_dispatch_checklist_kitsu_update(addon, event):
            return

        bubble_up_settings = get_bubble_up_settings(addon, event.project)
        if not bubble_up_settings:
            logging.debug(
                f"[{bundle_name}] [ayon-kitsu] No bubble-up settings found, skipping"
            )
            return

        # Parse enabled value - access as dictionary key, not attribute
        enabled_value = bubble_up_settings.get("enabled")
        logging.debug(
            f"[{bundle_name}] [ayon-kitsu] Raw enabled_value: {enabled_value} (type: {type(enabled_value)})"
        )

        if isinstance(enabled_value, str):
            enabled_value = enabled_value.lower() in ("true", "1", "yes", "on")
            logging.debug(
                f"[{bundle_name}] [ayon-kitsu] Parsed string enabled_value to: {enabled_value}"
            )
        elif enabled_value is None:
            enabled_value = False
            logging.debug(
                f"[{bundle_name}] [ayon-kitsu] enabled_value is None, defaulting to False"
            )

        logging.debug(
            f"[{bundle_name}] [ayon-kitsu] Final enabled_value: {enabled_value} (type: {type(enabled_value)})"
        )

        # Early exit if feature is disabled
        if not enabled_value:
            logging.debug(
                f"[{bundle_name}] [ayon-kitsu] Feature is disabled (enabled_value={enabled_value}), "
                f"skipping uniqueSprites bubble-up"
            )
            return

        # Normalize statuses and task types for case-insensitive matching
        # Access as dictionary keys, not attributes
        raw_statuses = bubble_up_settings.get("statuses", [])
        raw_task_types = bubble_up_settings.get("task_types", [])

        logging.debug(
            f"[{bundle_name}] [ayon-kitsu] Raw settings - statuses: {raw_statuses} (type: {type(raw_statuses)}), "
            f"task_types: {raw_task_types} (type: {type(raw_task_types)})"
        )

        # Ensure we have lists, not other types
        if not isinstance(raw_statuses, list):
            raw_statuses = [raw_statuses] if raw_statuses else []
        if not isinstance(raw_task_types, list):
            raw_task_types = [raw_task_types] if raw_task_types else []

        # Status shortnames: compare case-insensitively (uppercase)
        configured_statuses = [
            str(s).strip().upper() for s in raw_statuses if str(s).strip()
        ]
        # Task type names: compare case-insensitively (lowercase)
        configured_task_types = [
            str(t).strip().lower() for t in raw_task_types if str(t).strip()
        ]

        logging.debug(
            f"[{bundle_name}] [ayon-kitsu] Normalized settings - statuses: {configured_statuses}, "
            f"task_types: {configured_task_types}"
        )

        if not configured_statuses or not configured_task_types:
            logging.debug(
                f"[{bundle_name}] [ayon-kitsu] Missing configuration - "
                f"statuses empty: {not configured_statuses}, "
                f"task_types empty: {not configured_task_types}, skipping"
            )
            return

        # task_id: status_changed uses entityId; data_changed may use id or taskId
        task_id = (
            event.summary.get("entityId")
            or event.summary.get("id")
            or event.summary.get("taskId")
        )
        # newValue/oldValue (status_changed) or status (data_changed)
        new_status_raw = event.payload.get("newValue") or event.payload.get(
            "status"
        )
        new_status = (
            str(new_status_raw).strip() if new_status_raw is not None else None
        )
        old_status = event.payload.get("oldValue")

        # For data_changed / updated, resolve status if not in payload
        if not new_status and event.topic in (
            "entity.task.updated",
            "entity.task.data_changed",
        ):
            # Try multiple ways to get the status
            new_status = (
                event.summary.get("status")
                or event.payload.get("status")
                or event.summary.get("newValue")
                or event.payload.get("newValue")
            )

            # Check if status is in the updated fields (data_changed / updated)
            updated_fields = event.summary.get("updatedFields", []) or []
            if not new_status and task_id and "status" in updated_fields:
                # Get the task to check current status
                task_entity = get_task_entity(event.project, task_id)
                if task_entity:
                    new_status = task_entity.get("status")
                    logging.info(
                        f"[{bundle_name}] [ayon-kitsu] Extracted status from task entity: {new_status}"
                    )

            # If still no status, this might not be a status change event
            if not new_status:
                logging.debug(
                    f"[{bundle_name}] [ayon-kitsu] No status change detected in updated event, "
                    f"skipping. Payload keys: {list(event.payload.keys())}, "
                    f"summary keys: {list(event.summary.keys())}, "
                    f"updatedFields: {updated_fields}"
                )
                return

            logging.info(
                f"[{bundle_name}] [ayon-kitsu] Detected status change in updated event: {new_status}"
            )

        project_name = event.project

        if not all([task_id, new_status, project_name]):
            logging.warning(
                f"[{bundle_name}] [ayon-kitsu] Missing required event data: "
                f"task_id={task_id}, new_status={new_status}, projectName={project_name}"
            )
            logging.debug(
                f"[{bundle_name}] [ayon-kitsu] Event details - topic: {event.topic}, "
                f"summary keys: {list(event.summary.keys())}, payload keys: {list(event.payload.keys())}"
            )
            return

        # Check if this status change should trigger bubble-up (status: uppercase compare)
        new_status_upper = (new_status or "").upper()
        if new_status_upper not in configured_statuses:
            logging.debug(
                f"[{bundle_name}] [ayon-kitsu] Status '{new_status}' not in configured statuses {configured_statuses}, skipping"
            )
            return

        task_entity = get_task_entity(project_name, task_id)
        if not task_entity:
            logging.warning(
                f"[{bundle_name}] [ayon-kitsu] Could not find task entity for id {task_id}"
            )
            return

        # Task type from entity: lowercase for case-insensitive match with settings
        task_type = (task_entity.get("taskType") or {}).get(
            "name"
        ) or "unknown"
        task_type = str(task_type).strip().lower()

        # Get the product and task information
        task_name = task_entity.get("name", "Unknown")

        # Get kitsu_task_id from task data
        kitsu_task_id = task_entity.get("data", {}).get("kitsuId")
        if not kitsu_task_id:
            logging.debug(
                f"[{bundle_name}] [ayon-kitsu] Task {task_id} has no kitsuId, skipping Kitsu comment update"
            )
            return

        logging.debug(
            f"[{bundle_name}] [ayon-kitsu] Task details - "
            f"task: {task_name}, task_type: {task_type}"
        )

        # Check if this task type should trigger bubble-up
        if task_type not in configured_task_types:
            logging.debug(
                f"[{bundle_name}] [ayon-kitsu] Task type '{task_type}' not in configured types {configured_task_types}, skipping"
            )
            return

        product_entity = get_latest_review_product(project_name, task_id)
        if not product_entity:
            logging.debug(
                f"[{bundle_name}] [ayon-kitsu] No review product found for task {task_id}, skipping"
            )
            return

        product_name = product_entity.get("name", "Unknown")
        version_entity = get_latest_version(project_name, product_entity["id"])
        if not version_entity:
            logging.warning(
                f"[{bundle_name}] [ayon-kitsu] Could not find latest version for product {product_name}"
            )
            return

        unique_sprites = (
            version_entity.get("attrib", {}).get("uniqueSprites")
            or version_entity.get("data", {}).get("uniqueSprites")
            or version_entity.get("attrib", {}).get("maxUniqueSprites")
            or version_entity.get("data", {}).get("maxUniqueSprites")
        )
        if unique_sprites is None:
            logging.debug(
                f"[{bundle_name}] [ayon-kitsu] No uniqueSprites found in version data for {product_name}"
            )
            return
        if str(unique_sprites).strip() in ("", "0"):
            logging.debug(
                f"[{bundle_name}] [ayon-kitsu] uniqueSprites is 0 or empty for {product_name}, skipping comment update"
            )
            return

        # Get comment template settings
        settings = addon.get_project_settings(project_name)
        template_cfg = (
            settings.get("publish", {})
            .get("IntegrateKitsuNote", {})
            .get("custom_comment_template", {})
        )

        # Dispatch event to processor service for Kitsu operations
        # Processor service has gazu available and can perform the actual Kitsu operations
        try:
            await dispatch_event(
                "kitsu.comment_update_request",
                description="Update Kitsu comment with uniqueSprites",
                project=project_name,
                summary={
                    "task_id": task_id,
                    "kitsu_task_id": kitsu_task_id,
                    "product_name": product_name,
                    "task_name": task_name,
                    "unique_sprites": unique_sprites,
                    "new_status": new_status,
                    "old_status": old_status,
                    "version": version_entity.get("version", 1),
                },
                payload={
                    "template_cfg": template_cfg,
                },
            )
            logging.info(
                f"[{bundle_name}] [ayon-kitsu] Dispatched Kitsu comment update to processor service "
                f"for version {version_entity['id']} ({product_name}) with uniqueSprites={unique_sprites}"
            )
            logging.info(
                f"[{bundle_name}] [EVENT VIEWER] Kitsu comment update dispatched to processor for {product_name}"
            )
        except Exception as e:
            logging.error(
                f"[{bundle_name}] [ayon-kitsu] Failed to dispatch Kitsu comment update to processor: {e}"
            )
            logging.error(traceback.format_exc())
            logging.error(
                f"[{bundle_name}] [EVENT VIEWER] ERROR: Failed to dispatch Kitsu comment update for {product_name}: {e}"
            )

    except Exception as e:
        logging.error(
            f"[{bundle_name}] [ayon-kitsu] Error handling version status event: {e}"
        )
        logging.error(traceback.format_exc())


def get_bubble_up_settings(
    addon, project_name: str
) -> Optional[Dict[str, Any]]:
    """Get the bubble-up settings from the event handler configuration."""
    try:
        settings = addon.get_project_settings(project_name)
        bubble_up_settings = (
            settings.get("publish", {})
            .get("IntegrateKitsuNote", {})
            .get("unique_sprites_bubble_up", {})
        )

        logging.debug(
            f"[{os.getenv('AYON_BUNDLE_NAME', 'Unknown')}] [ayon-kitsu] Retrieved bubble-up settings: {bubble_up_settings}"
        )

        return bubble_up_settings
    except Exception as e:
        logging.error(
            f"[{os.getenv('AYON_BUNDLE_NAME', 'Unknown')}] [ayon-kitsu] Error getting bubble-up settings: {e}"
        )
        logging.error(traceback.format_exc())
        return None


def get_task_entity(
    project_name: str, task_id: str
) -> Optional[Dict[str, Any]]:
    try:
        import ayon_api

        return ayon_api.get_task_by_id(project_name, task_id)
    except Exception as exc:
        logging.error(
            f"[{os.getenv('AYON_BUNDLE_NAME', 'Unknown')}] [ayon-kitsu] Error getting task entity: {exc}"
        )
        logging.error(traceback.format_exc())
        return None


def get_latest_review_product(
    project_name: str, task_id: str
) -> Optional[Dict[str, Any]]:
    try:
        import ayon_api

        products = list(
            ayon_api.get_products(
                project_name,
                task_ids=[task_id],
                product_types=["review"],
            )
        )
        if not products:
            return None
        products.sort(key=lambda item: item.get("name", ""))
        return products[-1]
    except Exception as exc:
        logging.error(
            f"[{os.getenv('AYON_BUNDLE_NAME', 'Unknown')}] [ayon-kitsu] Error getting review product: {exc}"
        )
        logging.error(traceback.format_exc())
        return None


def get_latest_version(
    project_name: str, product_id: str
) -> Optional[Dict[str, Any]]:
    try:
        import ayon_api

        versions = list(
            ayon_api.get_versions(project_name, product_ids=[product_id])
        )
        if not versions:
            return None
        versions.sort(key=lambda item: item.get("version", 0))
        return versions[-1]
    except Exception as exc:
        logging.error(
            f"[{os.getenv('AYON_BUNDLE_NAME', 'Unknown')}] [ayon-kitsu] Error getting latest version: {exc}"
        )
        logging.error(traceback.format_exc())
        return None
