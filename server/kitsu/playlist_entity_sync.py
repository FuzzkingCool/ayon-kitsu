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


async def _default_entity_list_folder_id(
    ayon_user: "UserEntity",
    entity_dict: EntityDict,
    project_name: str,
) -> str | None:
    session = await Session.create(ayon_user)
    headers = {"Authorization": f"Bearer {session.token}"}
    base = entity_dict["ayon_server_url"].rstrip("/")
    url = f"{base}/api/projects/{project_name}/entityListFolders"
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        folder_json = response.json()
    folders = (folder_json or {}).get("folders") or []
    if not folders:
        log.warning(
            "playlist sync: project %s has no entity list folders; "
            "create one in AYON before syncing Kitsu playlists",
            project_name,
        )
        return None
    return folders[0]["id"]


async def sync_playlist(
    _addon: "KitsuAddon",
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

    list_folder_id = await _default_entity_list_folder_id(ayon_user, entity_dict, pn)
    if not list_folder_id:
        return

    existing_id = await _find_list_id_by_kitsu_playlist_id(pn, playlist_id)

    items = [{"entityId": fid} for fid in folder_ids]

    async with httpx.AsyncClient(timeout=120.0) as client:
        if existing_id:
            patch_url = f"{base}/api/projects/{pn}/lists/{existing_id}"
            patch_payload: dict[str, Any] = {"label": label}
            response = await client.patch(
                patch_url,
                content=json.dumps(patch_payload),
                headers={**headers, "Content-Type": "application/json"},
            )
            response.raise_for_status()
            items_url = f"{base}/api/projects/{pn}/lists/{existing_id}/items"
            items_payload = {"items": items, "mode": "replace"}
            response = await client.patch(
                items_url,
                content=json.dumps(items_payload),
                headers={**headers, "Content-Type": "application/json"},
            )
            response.raise_for_status()
            log.info("Updated AYON playlist list %s (Kitsu %s)", existing_id, playlist_id)
            return

        create_url = f"{base}/api/projects/{pn}/lists"
        create_payload: dict[str, Any] = {
            "entityListType": "generic",
            "entityListFolderId": list_folder_id,
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

    async with httpx.AsyncClient(timeout=60.0) as client:
        delete_url = f"{base}/api/projects/{pn}/lists/{existing_id}"
        response = await client.delete(delete_url, headers=headers)
        if response.status_code == 404:
            return
        response.raise_for_status()
    log.info("Deleted AYON playlist list %s (Kitsu %s)", existing_id, playlist_id)
