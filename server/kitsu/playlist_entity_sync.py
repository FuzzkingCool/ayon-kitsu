"""Kitsu playlist → AYON entity list (folder) sync from ``/push`` / ``/remove``.

The processor resolves Kitsu ``entity_id`` rows to AYON folder UUIDs and sends
``ordered_ayon_folder_ids`` on the playlist payload. This module performs HTTP
calls to the AYON REST API using the same service session pattern as
``sync_person`` in ``push.py``.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

import httpx
from ayon_server.auth.session import Session
from ayon_server.entities import ProjectEntity
from ayon_server.lib.postgres import Postgres

from .playlist_list_coercion import coerce_entity_list_data, kitsu_id_match_candidates

if TYPE_CHECKING:
    from ayon_server.entities import UserEntity

    from .. import KitsuAddon

EntityDict = dict[str, Any]

log = logging.getLogger("kitsu.playlist_entity_sync")

_HTTP_ERROR_TEXT_MAX = 2000


def _log_http_error(context: str, response: httpx.Response) -> None:
    err_text = (response.text or "")[:_HTTP_ERROR_TEXT_MAX]
    detail_note = ""
    try:
        parsed = json.loads(err_text)
        if isinstance(parsed, dict):
            detail = parsed.get("detail")
            if detail is not None and detail != parsed:
                detail_note = f" detail={detail!r}"
    except Exception:
        pass
    log.error(
        "%s: HTTP %s %s response_text=%r%s",
        context,
        response.status_code,
        str(response.request.url),
        err_text,
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
    candidates = kitsu_id_match_candidates(kitsu_playlist_id)
    if not candidates:
        return None
    for table in ("lists", "entity_lists"):
        try:
            rows = await Postgres.fetch(
                f"""
                SELECT id::text AS id
                FROM project_{project_name}.{table}
                WHERE data->>'kitsuId' = ANY($1::text[])
                LIMIT 1
                """,
                candidates,
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


def _uuid_dashed_for_pg(raw: str) -> str:
    """Normalize id to dashed lowercase UUID for ``::uuid`` casts in SQL."""
    s = str(raw).strip()
    h = s.lower().replace("-", "")
    if len(h) == 32 and all(c in "0123456789abcdef" for c in h):
        return str(uuid.UUID(h))
    return s


async def _find_list_id_for_playlist_reconcile(
    project_name: str,
    *,
    entity_list_folder_id: str,
    kitsu_playlist_id: str,
    label: str,
) -> str | None:
    """Find list row when ``data.kitsuId`` is missing but a row exists (folder + label).

    AYON ``entity_lists`` uses a unique index on ``label``; rows created outside
    Kitsu sync may omit ``data.kitsuId``, causing duplicate POSTs and 5xx.
    """
    by_kitsu = await _find_list_id_by_kitsu_playlist_id(project_name, kitsu_playlist_id)
    if by_kitsu:
        return by_kitsu
    folder_uuid = _uuid_dashed_for_pg(entity_list_folder_id)
    for table in ("lists", "entity_lists"):
        try:
            rows = await Postgres.fetch(
                f"""
                SELECT id::text AS id
                FROM project_{project_name}.{table}
                WHERE entity_list_folder_id = $1::uuid
                  AND label = $2
                  AND active IS TRUE
                  AND (data->>'kitsuId' IS NULL OR data->>'kitsuId' = '')
                LIMIT 2
                """,
                folder_uuid,
                label,
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "does not exist" in msg or "undefinedtable" in msg.replace(" ", ""):
                continue
            log.debug("playlist reconcile lookup %s: %s", table, exc)
            continue
        if len(rows) == 1:
            return rows[0]["id"]
        if len(rows) > 1:
            ids = [str(r["id"]) for r in rows]
            log.warning(
                "playlist reconcile: ambiguous rows in %s for folder=%s label=%r "
                "list_ids=%s",
                table,
                folder_uuid[:12],
                label[:80],
                [i[:12] for i in ids],
            )
    # Global label uniqueness (see AYON ``entity_lists_name`` index): duplicate POST
    # can fail even when the row lives under another folder id.
    for table in ("lists", "entity_lists"):
        try:
            rows = await Postgres.fetch(
                f"""
                SELECT id::text AS id, data, entity_list_folder_id::text AS efid
                FROM project_{project_name}.{table}
                WHERE label = $1
                  AND active IS TRUE
                LIMIT 2
                """,
                label,
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "does not exist" in msg or "undefinedtable" in msg.replace(" ", ""):
                continue
            log.debug("playlist reconcile label-only %s: %s", table, exc)
            continue
        if len(rows) > 1:
            ids = [str(r["id"]) for r in rows]
            log.warning(
                "playlist reconcile label-only: multiple rows in %s for label=%r "
                "list_ids=%s",
                table,
                label[:80],
                [i[:12] for i in ids],
            )
            continue
        if len(rows) != 1:
            continue
        row = rows[0]
        raw_data = row.get("data")
        data_dict = coerce_entity_list_data(raw_data)
        kid: str | None = None
        v = data_dict.get("kitsuId")
        kid = str(v) if v is not None and v != "" else None
        if kid not in (None, ""):
            kid_forms = set(kitsu_id_match_candidates(kid))
            want_forms = set(kitsu_id_match_candidates(str(kitsu_playlist_id)))
            if kid_forms.isdisjoint(want_forms):
                log.warning(
                    "playlist reconcile: label %r already tied to other kitsuId=%s "
                    "(not overwriting)",
                    label[:80],
                    str(kid)[:12],
                )
                continue
        row_folder = row.get("efid")
        if row_folder and _rest_uuid32(row_folder) != _rest_uuid32(folder_uuid):
            log.warning(
                "playlist reconcile: list %s label=%r exists under another folder "
                "(expected %s); adopting row to fix duplicate-label POST failure",
                str(row["id"])[:12],
                label[:80],
                str(folder_uuid)[:12],
            )
        return row["id"]
    return None


async def _ensure_list_kitsu_metadata(
    client: httpx.AsyncClient,
    list_url: str,
    headers: dict[str, str],
    kitsu_playlist_id: str,
) -> None:
    """PATCH ``data.kitsuId`` / ``kitsuSource`` when reconciling a ghost list."""
    get_full = await client.get(list_url, headers=headers)
    if not get_full.is_success:
        return
    full_doc = get_full.json()
    if not isinstance(full_doc, dict):
        return
    d = full_doc.get("data")
    if not isinstance(d, dict):
        d = {}
    if str(d.get("kitsuId") or "") == str(kitsu_playlist_id):
        return
    merged = {**d, "kitsuId": str(kitsu_playlist_id), "kitsuSource": "playlist"}
    response = await client.patch(
        list_url,
        content=json.dumps({"data": merged}),
        headers={**headers, "Content-Type": "application/json"},
    )
    if not response.is_success:
        _log_http_error("playlist sync PATCH list data (kitsuId)", response)


async def _apply_playlist_list_updates(
    client: httpx.AsyncClient,
    *,
    base: str,
    pn: str,
    playlist_id: str,
    label: str,
    items: list[dict[str, str]],
    list_id_raw: str,
    headers: dict[str, str],
) -> None:
    """PATCH label, items, and kitsu metadata for an existing entity list."""
    list_rest_id = _rest_uuid32(list_id_raw)
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
        meta_doc = get_resp.json()
        if isinstance(meta_doc, dict):
            cur = meta_doc.get("label")
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
            log.error(
                "playlist sync PATCH list items failed project=%s "
                "kitsu_playlist_id=%s list_id=%s item_count=%d url=%s http=%s",
                pn,
                playlist_id,
                list_rest_id,
                len(items),
                items_url,
                response.status_code,
            )
            _log_http_error("playlist sync PATCH list items", response)
            response.raise_for_status()
    else:
        log.debug(
            "playlist sync: skip PATCH list items (empty) list=%s Kitsu=%s",
            list_rest_id,
            playlist_id,
        )
    await _ensure_list_kitsu_metadata(client, patch_url, headers, playlist_id)
    log.info("Updated AYON playlist list %s (Kitsu %s)", list_rest_id, playlist_id)


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

        list_row_id = existing_id
        if not list_row_id and items:
            list_row_id = await _find_list_id_for_playlist_reconcile(
                pn,
                entity_list_folder_id=list_folder_id,
                kitsu_playlist_id=str(playlist_id),
                label=label,
            )
            if list_row_id:
                log.info(
                    "playlist sync: reconciled AYON list %s for Kitsu playlist %s "
                    "(folder+label, missing data.kitsuId)",
                    str(list_row_id)[:12],
                    playlist_id,
                )

        if list_row_id:
            await _apply_playlist_list_updates(
                client,
                base=base,
                pn=pn,
                playlist_id=str(playlist_id),
                label=label,
                items=items,
                list_id_raw=list_row_id,
                headers=headers,
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
            if response.status_code == 409:
                log.warning(
                    "playlist sync POST list returned 409 Conflict project=%s "
                    "kitsu_playlist_id=%s label=%r (likely duplicate label); "
                    "attempting reconcile+PATCH",
                    pn,
                    playlist_id,
                    label[:120],
                )
            log.error(
                "playlist sync POST list failed project=%s kitsu_playlist_id=%s "
                "label=%r entity_list_folder_id=%s item_count=%d url=%s http=%s",
                pn,
                playlist_id,
                label[:120],
                folder_rest,
                len(items),
                create_url,
                response.status_code,
            )
            _log_http_error("playlist sync POST list", response)
            log.error(
                "playlist sync POST payload summary: entityListFolderId=%s "
                "label_len=%d item_count=%d first_entity_id=%s "
                "(if http=500: often duplicate label or DB constraint — check AYON "
                "server logs for POST /lists; ghost rows without data.kitsuId are "
                "reconciled on retry)",
                folder_rest,
                len(label),
                len(items),
                (items[0].get("entityId") if items else None),
            )
            rebound = await _find_list_id_for_playlist_reconcile(
                pn,
                entity_list_folder_id=list_folder_id,
                kitsu_playlist_id=str(playlist_id),
                label=label,
            )
            if rebound:
                log.warning(
                    "playlist sync: POST failed http=%s; found existing list %s — "
                    "applying PATCH (reconcile after failure).",
                    response.status_code,
                    str(rebound)[:12],
                )
                await _apply_playlist_list_updates(
                    client,
                    base=base,
                    pn=pn,
                    playlist_id=str(playlist_id),
                    label=label,
                    items=items,
                    list_id_raw=rebound,
                    headers=headers,
                )
                return
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
