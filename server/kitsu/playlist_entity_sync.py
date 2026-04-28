"""Kitsu playlist → AYON entity list (folder) sync from ``/push`` / ``/remove``.

The processor resolves Kitsu ``entity_id`` rows to AYON folder UUIDs and sends
``ordered_ayon_folder_ids`` on the playlist payload. This module performs HTTP
calls to the AYON REST API using the same service session pattern as
``sync_person`` in ``push.py``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import httpx
from ayon_server.auth.session import Session
from ayon_server.entities import ProjectEntity
from ayon_server.lib.postgres import Postgres

if TYPE_CHECKING:
    from ayon_server.entities import UserEntity

    from .. import KitsuAddon

EntityDict = dict[str, Any]

log = logging.getLogger("kitsu.playlist_entity_sync")

_HTTP_ERROR_BODY_MAX = 2000


def _log_http_error(context: str, response: httpx.Response) -> None:
    body = (response.text or "")[:_HTTP_ERROR_BODY_MAX]
    detail_note = ""
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            detail = parsed.get("detail")
            if detail is not None and detail != parsed:
                detail_note = f" detail={detail!r}"
    except Exception:
        pass
    log.error(
        "%s: HTTP %s %s body=%r%s",
        context,
        response.status_code,
        str(response.request.url),
        body,
        detail_note,
    )


def _rest_uuid32(raw_id: str | None) -> str | None:
    """32 lowercase hex (no hyphens) for AYON REST list paths and entity IDs.

    OpenAPI commonly expects this shape; Postgres ``id::text`` is often dashed.
    """
    if raw_id is None:
        return None
    s = str(raw_id).strip().lower().replace("-", "")
    if len(s) == 32 and all(c in "0123456789abcdef" for c in s):
        return s
    return str(raw_id).strip()


def _list_item_rows_from_folder_ids(folder_ids: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for fid in folder_ids:
        nid = _rest_uuid32(fid) or str(fid).strip()
        if nid:
            rows.append({"entityId": nid})
    return rows


async def _find_list_id_by_kitsu_playlist_id(
    project_name: str, kitsu_playlist_id: str
) -> str | None:
    """Resolve AYON entity list id from ``data.kitsuId`` (Kitsu playlist id)."""
    for table in ("lists", "entity_lists"):
        try:
            rows = await Postgres.fetch(
                f"""
                SELECT id::text AS id
                FROM project_{project_name}.{table}
                WHERE data->>'kitsuId' = $1
                LIMIT 1
                """,
                kitsu_playlist_id,
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "does not exist" in msg or "undefinedtable" in msg.replace(" ", ""):
                continue
            log.debug("playlist list lookup %s: %s", table, exc)
            continue
        if rows:
            return rows[0]["id"]
    return None


async def _ensure_entity_list_folder_id(
    client: httpx.AsyncClient,
    addon: "KitsuAddon",
    ayon_user: "UserEntity",
    entity_dict: EntityDict,
    project_name: str,
    headers: dict[str, str],
    base: str,
) -> str | None:
    """Return first entity-list folder id, or create one when settings allow."""
    settings = await addon.get_studio_settings()
    ps = getattr(settings.sync_settings, "playlist_sync", None)
    auto_create = (
        True if ps is None else getattr(ps, "auto_create_entity_list_folder", True)
    )
    folder_label = "Kitsu playlists"
    if ps is not None:
        folder_label = (getattr(ps, "list_folder_label", None) or folder_label).strip()
    if not folder_label:
        folder_label = "Kitsu playlists"

    url = f"{base}/api/projects/{project_name}/entityListFolders"
    response = await client.get(url, headers=headers)
    if not response.is_success:
        _log_http_error("playlist sync GET entityListFolders", response)
        response.raise_for_status()
    folder_json = response.json()
    folders = (folder_json or {}).get("folders") or []
    if folders:
        return folders[0]["id"]

    if not auto_create:
        log.warning(
            "playlist sync: project %s has no entity list folders; "
            "enable auto_create_entity_list_folder or create a folder in AYON",
            project_name,
        )
        return None

    create_payload = {"label": folder_label}
    response = await client.post(
        url,
        content=json.dumps(create_payload),
        headers={**headers, "Content-Type": "application/json"},
    )
    if not response.is_success:
        _log_http_error("playlist sync POST entityListFolders", response)
        response.raise_for_status()
    created = response.json()
    new_id = created.get("id") if isinstance(created, dict) else created
    log.info(
        "playlist sync: created default entity list folder %r for project %s",
        new_id,
        project_name,
    )
    return str(new_id) if new_id else None


async def sync_playlist(
    addon: "KitsuAddon",
    ayon_user: "UserEntity",
    project: ProjectEntity,
    entity_dict: EntityDict,
) -> None:
    """Upsert an AYON folder-type entity list from a Kitsu playlist payload."""
    playlist_id = entity_dict.get("id")
    if not playlist_id:
        log.warning("playlist sync: missing playlist id, skipping")
        return
    label = (entity_dict.get("name") or "Playlist").strip() or "Playlist"
    folder_ids: list[str] = list(entity_dict.get("ordered_ayon_folder_ids") or [])
    session = await Session.create(ayon_user)
    headers = {"Authorization": f"Bearer {session.token}"}
    base = entity_dict["ayon_server_url"].rstrip("/")
    pn = project.name

    existing_id = await _find_list_id_by_kitsu_playlist_id(pn, playlist_id)

    items = _list_item_rows_from_folder_ids(folder_ids)

    if not existing_id and not items:
        log.info(
            "playlist sync: skip create for Kitsu playlist %s (%r): "
            "no resolved AYON folder members (empty ordered_ayon_folder_ids)",
            playlist_id,
            label[:80],
        )
        return

    async with httpx.AsyncClient(timeout=120.0) as client:
        list_folder_id = await _ensure_entity_list_folder_id(
            client, addon, ayon_user, entity_dict, pn, headers, base
        )
        if not list_folder_id:
            return

        if existing_id:
            list_rest_id = _rest_uuid32(existing_id)
            if not list_rest_id:
                log.warning(
                    "playlist sync: invalid list id from DB for Kitsu %s, skipping update",
                    playlist_id,
                )
                return
            patch_url = f"{base}/api/projects/{pn}/lists/{list_rest_id}"
            meta_url = patch_url
            get_resp = await client.get(
                meta_url,
                headers=headers,
                params={"metadata_only": "true"},
            )
            current_label: str | None = None
            if get_resp.is_success:
                meta_body = get_resp.json()
                if isinstance(meta_body, dict):
                    cur = meta_body.get("label")
                    current_label = cur if isinstance(cur, str) else None

            if current_label != label:
                patch_payload: dict[str, Any] = {"label": label}
                response = await client.patch(
                    patch_url,
                    content=json.dumps(patch_payload),
                    headers={**headers, "Content-Type": "application/json"},
                )
                if not response.is_success:
                    _log_http_error("playlist sync PATCH list label", response)
                    log.warning(
                        "playlist sync: label PATCH failed for list %s (Kitsu %s); "
                        "continuing with items PATCH",
                        list_rest_id,
                        playlist_id,
                    )
            items_url = f"{base}/api/projects/{pn}/lists/{list_rest_id}/items"
            if items:
                items_payload = {"items": items, "mode": "replace"}
                response = await client.patch(
                    items_url,
                    content=json.dumps(items_payload),
                    headers={**headers, "Content-Type": "application/json"},
                )
                if not response.is_success:
                    _log_http_error("playlist sync PATCH list items", response)
                    response.raise_for_status()
            else:
                log.debug(
                    "playlist sync: skip PATCH list items (empty) list=%s Kitsu=%s",
                    list_rest_id,
                    playlist_id,
                )
            log.info(
                "Updated AYON playlist list %s (Kitsu %s)", list_rest_id, playlist_id
            )
            return

        create_url = f"{base}/api/projects/{pn}/lists"
        folder_rest = _rest_uuid32(list_folder_id) or list_folder_id
        create_payload: dict[str, Any] = {
            "entityListType": "generic",
            "entityListFolderId": folder_rest,
            "entityType": "folder",
            "label": label,
            "data": {"kitsuId": playlist_id, "kitsuSource": "playlist"},
            "active": True,
            "items": items,
        }
        response = await client.post(
            create_url,
            content=json.dumps(create_payload),
            headers={**headers, "Content-Type": "application/json"},
        )
        if not response.is_success:
            _log_http_error("playlist sync POST list", response)
            response.raise_for_status()
        created = response.json()
        if isinstance(created, dict):
            new_id = created.get("id")
        else:
            new_id = created
        log.info(
            "Created AYON playlist list %r for Kitsu playlist %s",
            new_id,
            playlist_id,
        )


async def delete_playlist(
    _addon: "KitsuAddon",
    ayon_user: "UserEntity",
    project: ProjectEntity,
    entity_dict: EntityDict,
) -> None:
    """Remove the AYON entity list paired to a Kitsu playlist id."""
    playlist_id = entity_dict.get("id")
    if not playlist_id:
        return
    session = await Session.create(ayon_user)
    headers = {"Authorization": f"Bearer {session.token}"}
    base = entity_dict["ayon_server_url"].rstrip("/")
    pn = project.name

    existing_id = await _find_list_id_by_kitsu_playlist_id(pn, playlist_id)
    if not existing_id:
        log.debug("playlist delete: no AYON list for Kitsu playlist %s", playlist_id)
        return
    list_rest_id = _rest_uuid32(existing_id)
    if not list_rest_id:
        log.warning(
            "playlist delete: invalid list id for Kitsu playlist %s, skipping",
            playlist_id,
        )
        return

    async with httpx.AsyncClient(timeout=60.0) as client:
        delete_url = f"{base}/api/projects/{pn}/lists/{list_rest_id}"
        response = await client.delete(delete_url, headers=headers)
        if response.status_code == 404:
            return
        if not response.is_success:
            _log_http_error("playlist sync DELETE list", response)
        response.raise_for_status()
    log.info("Deleted AYON playlist list %s (Kitsu %s)", list_rest_id, playlist_id)
