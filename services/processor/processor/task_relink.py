"""Relink stale data.kitsuId on AYON tasks and asset folders using ayon_api only."""

from __future__ import annotations

import time
from typing import Any

import ayon_api
from ayon_api.exceptions import HTTPRequestError
from nxtools import logging, slugify

from .concept_expand import (
    per_linked_effective_linked_id_for_relink,
    should_attempt_per_linked_concept_relink,
)

_FOLDER_SCAN_MAX_ATTEMPTS = 3
_FOLDER_SCAN_RETRY_SLEEP_SEC = 1.5
_RETRYABLE_FOLDER_SCAN_STATUSES = frozenset((502, 503, 504))


def _http_status_from_folder_scan_error(exc: BaseException) -> int | None:
    if isinstance(exc, HTTPRequestError) and exc.response is not None:
        return getattr(exc.response, "status_code", None)
    resp = getattr(exc, "response", None)
    if resp is not None:
        return getattr(resp, "status_code", None)
    return None


def _is_retryable_folder_scan_error(exc: BaseException) -> bool:
    """Avoid importing ``requests.exceptions`` tuples (minimal stubs / import order)."""
    if isinstance(exc, HTTPRequestError):
        st = _http_status_from_folder_scan_error(exc)
        return st is not None and st in _RETRYABLE_FOLDER_SCAN_STATUSES
    n = type(exc).__name__
    if n in (
        "ConnectionError",
        "ReadTimeoutError",
        "ConnectTimeout",
        "ConnectTimeoutError",
        "TimeoutError",
        "ChunkedEncodingError",
        "ProtocolError",
    ):
        return True
    st = _http_status_from_folder_scan_error(exc)
    return st is not None and st in _RETRYABLE_FOLDER_SCAN_STATUSES


def _list_active_folders(project_name: str) -> list[Any]:
    """Materialize active folders with retries on transient GraphQL / gateway errors."""
    for attempt in range(_FOLDER_SCAN_MAX_ATTEMPTS):
        try:
            return list(ayon_api.get_folders(project_name, active=True))
        except BaseException as exc:
            if (
                attempt + 1 >= _FOLDER_SCAN_MAX_ATTEMPTS
                or not _is_retryable_folder_scan_error(exc)
            ):
                raise
            logging.warning(
                "[task_relink] get_folders failed project=%s attempt=%s/%s: %s",
                project_name,
                attempt + 1,
                _FOLDER_SCAN_MAX_ATTEMPTS,
                exc,
            )
            time.sleep(_FOLDER_SCAN_RETRY_SLEEP_SEC * (attempt + 1))
    assert False, "unreachable"


def merge_push_response_folder_map(
    folder_map: dict[str, str], response_data: dict[str, Any] | None
) -> None:
    """Merge kitsu entity id -> AYON folder id from a /push response body."""
    if not response_data:
        return
    folders = response_data.get("folders")
    if isinstance(folders, dict):
        folder_map.update(folders)


def kitsu_folder_map_from_ayon_project(project_name: str) -> dict[str, str]:
    """Build Kitsu id → AYON folder id from ``data.kitsuId`` on all active folders."""
    out: dict[str, str] = {}
    for folder in _list_active_folders(project_name):
        data = folder.get("data") or {}
        kid = data.get("kitsuId")
        if kid:
            out[str(kid)] = str(folder["id"])
    return out


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
    for folder in _list_active_folders(project_name):
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


def is_folder_unique_violation(exc: BaseException) -> bool:
    """True if error indicates duplicate AYON folder (parent_id + name)."""
    msg = str(exc).lower()
    if "folder with parent_id" in msg and "already exists" in msg:
        return True
    if hasattr(exc, "response") and exc.response is not None:
        try:
            body = exc.response.json()
            if isinstance(body, dict):
                det = str(body.get("detail", "")).lower()
                err = str(body.get("error", "")).lower()
                if "folder with parent_id" in det and "already exists" in det:
                    return True
                if err == "unique-violation" and "folder" in det and "already exists" in det:
                    return True
        except Exception:
            pass
    return False


def _ayon_asset_folder_name(kitsu_name: str) -> str:
    """Match server/kitsu/utils.create_name_and_label slug for folder.name."""
    return slugify(kitsu_name, separator="_")


def try_relink_per_linked_concept_folder_on_unique_violation(
    project_name: str,
    concept_entity: dict[str, Any],
    folder_map: dict[str, str],
    concept_sync: dict[str, Any] | None = None,
) -> bool:
    """After a 409 on Concept create, adopt a sibling slug folder for ``per_linked_entity``.

    Typical cause: duplicate push rows for the same linked id created a folder on
    the first attempt; a later attempt collides on ``(parent_id, name)``. Another
    case: a legacy folder still uses a Kitsu **concept** id as ``data.kitsuId`` but
    the slug already matches this linked entity's title.
    """
    linked_id = per_linked_effective_linked_id_for_relink(concept_entity, concept_sync)
    if not linked_id:
        return False
    if folder_map.get(linked_id):
        return True

    from . import concept_naming

    parent_kitsu = concept_entity.get("parent_id")
    if parent_kitsu:
        parent_ayon = find_folder_id_for_kitsu_entity(
            project_name,
            str(parent_kitsu),
            folder_map,
        )
    else:
        parent_ayon = find_folder_id_for_kitsu_entity(
            project_name,
            "concept",
            folder_map,
        )
    if not parent_ayon:
        logging.warning(
            "[concept_relink] No AYON parent for per-linked Concept "
            f"(linked_id={linked_id[:8]}… parent_kitsu={parent_kitsu!r})"
        )
        return False

    title = concept_naming.concept_primary_title_for_folder(concept_entity)
    expected_name = slugify((title or "").strip() or "concept", separator="_") or (
        "concept"
    )

    candidate = None
    for folder in _list_active_folders(project_name):
        if str(folder.get("parentId") or "") != str(parent_ayon):
            continue
        if folder.get("folderType") != "Concept":
            continue
        if folder.get("name") != expected_name:
            continue
        candidate = folder
        break

    if not candidate:
        return False

    current = (candidate.get("data") or {}).get("kitsuId")
    cur_s = str(current or "").strip()
    sources = [
        str(x) for x in (concept_entity.get("kitsuSourceConceptIds") or []) if x
    ]
    row_cid = str(concept_entity.get("id") or "").strip()
    if row_cid and row_cid != linked_id and row_cid not in sources:
        sources.insert(0, row_cid)
    if cur_s and cur_s != linked_id:
        if sources and cur_s not in sources:
            return False

    new_data = dict(candidate.get("data") or {})
    new_data["kitsuId"] = linked_id
    merged: list[str] = []
    prev_src = new_data.get("kitsuSourceConceptIds")
    if isinstance(prev_src, list):
        merged.extend(str(x) for x in prev_src if x)
    for s in sources:
        if s not in merged:
            merged.append(s)
    new_data["kitsuSourceConceptIds"] = merged[:50]
    ayon_api.update_folder(
        project_name,
        str(candidate["id"]),
        data=new_data,
    )
    folder_map[linked_id] = str(candidate["id"])
    logging.info(
        f"[concept_relink] Retargeted AYON Concept folder "
        f"{str(candidate['id'])[:8]}… -> kitsuId={linked_id[:8]}… "
        f"(was {(cur_s or '')[:8]}…, slug={expected_name!r})"
    )
    return True


def try_relink_stale_kitsu_asset_folder(
    project_name: str,
    asset_entity: dict[str, Any],
    folder_map: dict[str, str],
) -> bool:
    """If AYON has a folder under the type parent with matching slug but wrong kitsuId, fix data.

    Returns True if ayon_api.update_folder was applied.
    """
    if asset_entity.get("type") != "Asset":
        return False
    kitsu_asset_id = asset_entity.get("id")
    entity_type_id = asset_entity.get("entity_type_id")
    kitsu_name = asset_entity.get("name")
    if not kitsu_asset_id or not entity_type_id or not kitsu_name:
        return False

    parent_id = folder_map.get(entity_type_id)
    if not parent_id:
        parent_id = find_folder_id_for_kitsu_entity(
            project_name, entity_type_id, folder_map
        )
    if not parent_id:
        logging.warning(
            f"[folder_relink] No AYON parent folder for asset type "
            f"{entity_type_id!r}, skip relink"
        )
        return False

    expected_name = _ayon_asset_folder_name(kitsu_name)
    candidate = None
    for folder in _list_active_folders(project_name):
        if folder.get("parentId") != parent_id:
            continue
        if folder.get("name") != expected_name:
            continue
        candidate = folder
        break

    if not candidate:
        return False

    current_kitsu = (candidate.get("data") or {}).get("kitsuId")
    if current_kitsu and _normalize_uuid(current_kitsu) == _normalize_uuid(
        kitsu_asset_id
    ):
        return False

    new_data = {**(candidate.get("data") or {}), "kitsuId": kitsu_asset_id}
    ayon_api.update_folder(
        project_name,
        candidate["id"],
        data=new_data,
    )
    logging.info(
        f"[folder_relink] Relinked AYON folder {candidate['id']!r} "
        f"kitsuId {current_kitsu!r} -> {kitsu_asset_id!r} "
        f"(parent={parent_id!r}, name={expected_name!r})"
    )
    return True


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
    concept_sync: dict[str, Any] | None = None,
):
    """POST /push; relink stale kitsuId once on task or asset folder unique violation."""
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
        if (
            entity.get("type") == "Asset"
            and is_folder_unique_violation(e)
            and try_relink_stale_kitsu_asset_folder(
                project_name, entity, folder_map
            )
        ):
            response2 = ayon_api.post(
                f"{entrypoint}/push",
                project_name=project_name,
                entities=entities,
            )
            response2.raise_for_status()
            merge_push_response_folder_map(folder_map, response2.data)
            return response2
        if (
            entity.get("type") == "Concept"
            and is_folder_unique_violation(e)
            and should_attempt_per_linked_concept_relink(entity, concept_sync)
            and try_relink_per_linked_concept_folder_on_unique_violation(
                project_name, entity, folder_map, concept_sync=concept_sync,
            )
        ):
            folder_map.update(kitsu_folder_map_from_ayon_project(project_name))
            response2 = ayon_api.post(
                f"{entrypoint}/push",
                project_name=project_name,
                entities=entities,
            )
            response2.raise_for_status()
            merge_push_response_folder_map(folder_map, response2.data)
            return response2
        raise


def push_batch_with_relink(
    entrypoint: str,
    project_name: str,
    entities: list[dict[str, Any]],
    folder_map: dict[str, str],
    concept_sync: dict[str, Any] | None = None,
):
    """POST /push for a batch; on folder unique-violation, relink per-linked Concepts once.

    Fullsync sends large batches; the server may reject with a folder collision before
    ``push_entities_with_relink``'s single-entity relink runs. After a batch failure,
    try ``try_relink_per_linked_concept_folder_on_unique_violation`` for every
    ``per_linked_entity`` Concept in the batch, refresh ``folder_map``, then retry
    the same batch once.
    """
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
        if len(entities) <= 1:
            raise
        if not is_folder_unique_violation(e):
            raise
        touched = False
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            if (
                ent.get("type") == "Concept"
                and should_attempt_per_linked_concept_relink(ent, concept_sync)
                and try_relink_per_linked_concept_folder_on_unique_violation(
                    project_name, ent, folder_map, concept_sync=concept_sync,
                )
            ):
                touched = True
        if not touched:
            raise
        folder_map.update(kitsu_folder_map_from_ayon_project(project_name))
        response2 = ayon_api.post(
            f"{entrypoint}/push",
            project_name=project_name,
            entities=entities,
        )
        response2.raise_for_status()
        merge_push_response_folder_map(folder_map, response2.data)
        return response2
