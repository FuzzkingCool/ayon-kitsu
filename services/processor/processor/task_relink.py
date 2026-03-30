"""Relink stale data.kitsuId on AYON tasks using ayon_api only (no Postgres)."""

from __future__ import annotations

from typing import Any

import ayon_api
from nxtools import logging


def merge_push_response_folder_map(
    folder_map: dict[str, str], response_data: dict[str, Any] | None
) -> None:
    """Merge kitsu entity id -> AYON folder id from a /push response body."""
    if not response_data:
        return
    folders = response_data.get("folders")
    if isinstance(folders, dict):
        folder_map.update(folders)


def find_folder_id_for_kitsu_entity(
    project_name: str,
    kitsu_entity_id: str,
    folder_map: dict[str, str],
) -> str | None:
    if not kitsu_entity_id:
        return None
    mapped = folder_map.get(kitsu_entity_id)
    if mapped:
        return mapped
    for folder in ayon_api.get_folders(project_name, active=True):
        data = folder.get("data") or {}
        if data.get("kitsuId") == kitsu_entity_id:
            return folder.get("id")
    return None


def _normalize_uuid(value: str | None) -> str:
    return (value or "").replace("-", "").lower()


def is_task_unique_violation(exc: BaseException) -> bool:
    """True if error indicates duplicate AYON task (folder + name)."""
    msg = str(exc).lower()
    if "unique-violation" in msg:
        return True
    if "task with folder_id" in msg and "already exists" in msg:
        return True
    if hasattr(exc, "response") and exc.response is not None:
        try:
            body = exc.response.json()
            if isinstance(body, dict):
                det = str(body.get("detail", "")).lower()
                err = str(body.get("error", "")).lower()
                if "unique" in err or "already exists" in det:
                    return True
        except Exception:
            pass
    return False


def try_relink_stale_kitsu_task(
    project_name: str,
    task_entity: dict[str, Any],
    folder_map: dict[str, str],
) -> bool:
    """If AYON has exactly one matching task with wrong/missing kitsuId, set data.kitsuId.

    Returns True if ayon_api.update_task was applied.
    """
    if task_entity.get("type") != "Task":
        return False
    kitsu_task_id = task_entity.get("id")
    entity_id = task_entity.get("entity_id")
    name = task_entity.get("name")
    task_type_name = task_entity.get("task_type_name")
    if not kitsu_task_id or not entity_id or not name or not task_type_name:
        return False

    folder_id = find_folder_id_for_kitsu_entity(
        project_name, entity_id, folder_map
    )
    if not folder_id:
        logging.warning(
            f"[task_relink] No AYON folder for Kitsu entity {entity_id}, skip relink"
        )
        return False

    folder = ayon_api.get_folder_by_id(project_name, folder_id)
    if not folder or not folder.get("path"):
        return False
    folder_path = folder["path"]

    matches = ayon_api.get_tasks_by_folder_path(
        project_name,
        folder_path,
        task_names=[name],
        task_types=[task_type_name],
        fields={"id", "name", "taskType", "data"},
    )
    if len(matches) > 1:
        logging.error(
            f"[task_relink] Ambiguous: {len(matches)} tasks {name!r} / "
            f"{task_type_name!r} under {folder_path}, skip relink"
        )
        return False
    if len(matches) == 0:
        return False

    task = matches[0]
    current_kitsu = (task.get("data") or {}).get("kitsuId")
    if current_kitsu and _normalize_uuid(current_kitsu) == _normalize_uuid(
        kitsu_task_id
    ):
        return False

    new_data = {**(task.get("data") or {}), "kitsuId": kitsu_task_id}
    ayon_api.update_task(project_name, task["id"], data=new_data)
    logging.info(
        f"[task_relink] Relinked AYON task {task['id']!r} "
        f"kitsuId {current_kitsu!r} -> {kitsu_task_id!r} ({folder_path}/{name})"
    )
    return True


def push_entities_with_relink(
    entrypoint: str,
    project_name: str,
    entities: list[dict[str, Any]],
    folder_map: dict[str, str],
):
    """POST /push; for a single Task, relink stale kitsuId once on unique violation."""
    response = ayon_api.post(
        f"{entrypoint}/push",
        project_name=project_name,
        entities=entities,
    )
    try:
        response.raise_for_status()
        merge_push_response_folder_map(folder_map, response.data)
        return response
    except Exception as e:
        if len(entities) != 1:
            raise
        entity = entities[0]
        if (
            entity.get("type") == "Task"
            and is_task_unique_violation(e)
            and try_relink_stale_kitsu_task(project_name, entity, folder_map)
        ):
            response2 = ayon_api.post(
                f"{entrypoint}/push",
                project_name=project_name,
                entities=entities,
            )
            response2.raise_for_status()
            merge_push_response_folder_map(folder_map, response2.data)
            return response2
        raise
