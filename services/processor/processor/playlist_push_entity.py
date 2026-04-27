"""Build Kitsu ``Playlist`` payloads for ``POST .../push`` (server playlist_entity_sync)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import ayon_api
import gazu

from .playlist_order import ordered_kitsu_entity_ids_from_playlist
from .task_relink import find_folder_id_for_kitsu_entity

if TYPE_CHECKING:
    from .processor import KitsuProcessor

log = logging.getLogger("playlist_push_entity")

_HTTP_ERROR_BODY_MAX = 2000


def _playlist_settings(processor: "KitsuProcessor") -> dict[str, Any]:
    return (processor.settings.get("sync_settings") or {}).get("playlist_sync") or {}


def playlist_sync_enabled(processor: "KitsuProcessor") -> bool:
    return bool(_playlist_settings(processor).get("enabled"))


def iter_playlists_for_kitsu_project(kitsu_project_id: str):
    """Yield playlist dicts for a Kitsu project (all pages)."""
    page = 1
    while True:
        batch = gazu.playlist.all_playlists_for_project(
            kitsu_project_id, page=page
        )
        if not batch:
            break
        for pl in batch:
            yield pl
        page += 1


def ordered_kitsu_member_ids_for_push(playlist: dict[str, Any]) -> list[str]:
    """Kitsu shot/entity ids in playlist order, with ``all_shots_for_playlist`` fallback."""
    ids = ordered_kitsu_entity_ids_from_playlist(playlist)
    if ids:
        return ids
    if not playlist.get("id"):
        return []
    try:
        shots = gazu.playlist.all_shots_for_playlist(playlist)
    except Exception as exc:
        log.debug(
            "playlist %s: all_shots_for_playlist fallback failed: %s",
            str(playlist.get("id", "?"))[:8],
            exc,
        )
        return []
    out: list[str] = []
    for s in shots or []:
        if isinstance(s, dict) and s.get("id"):
            out.append(str(s["id"]))
    return out


def build_playlist_push_entity(
    project_name: str,
    playlist: dict[str, Any],
    kitsu_folder_map: dict[str, str],
    ayon_base_url: str,
) -> dict[str, Any]:
    """Resolve members to AYON folder ids and build a ``Playlist`` /push entity."""
    label = (playlist.get("name") or "Playlist").strip() or "Playlist"
    ordered: list[str] = []
    for kid in ordered_kitsu_member_ids_for_push(playlist):
        fid = kitsu_folder_map.get(kid) or find_folder_id_for_kitsu_entity(
            project_name, kid, kitsu_folder_map
        )
        if fid:
            ordered.append(fid)
        else:
            log.debug(
                "playlist %s: skip member %s (no AYON folder with data.kitsuId)",
                playlist.get("id", "?")[:8],
                kid[:8] if kid else "?",
            )
    if not ordered:
        log.warning(
            "playlist %s (%r): zero AYON folder members resolved "
            "(check Kitsu shots rows / sync and folder data.kitsuId)",
            str(playlist.get("id", "?"))[:12],
            label[:64],
        )
    return {
        "type": "Playlist",
        "id": playlist["id"],
        "name": label,
        "ordered_ayon_folder_ids": ordered,
        "ayon_server_url": ayon_base_url,
    }


def _post_playlist_entities(
    processor: "KitsuProcessor", project_name: str, entities: list[dict[str, Any]]
) -> None:
    response = ayon_api.post(
        f"{processor.entrypoint}/push",
        project_name=project_name,
        entities=entities,
    )
    if not response.ok:
        body = (getattr(response, "text", None) or "")[:_HTTP_ERROR_BODY_MAX]
        log.error(
            "playlist push HTTP %s %s body=%r",
            response.status_code,
            getattr(response, "url", ""),
            body,
        )
    response.raise_for_status()


def sync_playlists_via_push_for_project(
    processor: "KitsuProcessor",
    kitsu_project_id: str,
    project_name: str,
    kitsu_folder_map: dict[str, str],
) -> dict[str, int]:
    """POST all Kitsu playlists for a project after structural folder sync.

    Returns counters for logging: ``fetched``, ``pushed``, ``fetch_failed``,
    ``push_failed``, ``zero_members``.
    """
    stats = {
        "fetched": 0,
        "pushed": 0,
        "fetch_failed": 0,
        "push_failed": 0,
        "zero_members": 0,
    }
    if not playlist_sync_enabled(processor):
        log.info(
            "[fullsync] playlist sync skipped project=%s (playlist_sync.enabled=false)",
            project_name,
        )
        return stats

    base = ayon_api.get_base_url()
    for pl in iter_playlists_for_kitsu_project(kitsu_project_id):
        try:
            full = gazu.playlist.get_playlist(pl["id"])
        except Exception as exc:
            log.warning("playlist fetch %s: %s", pl.get("id"), exc)
            stats["fetch_failed"] += 1
            continue
        stats["fetched"] += 1
        entity = build_playlist_push_entity(
            project_name, full, kitsu_folder_map, base
        )
        if not entity.get("ordered_ayon_folder_ids"):
            stats["zero_members"] += 1
        try:
            _post_playlist_entities(processor, project_name, [entity])
        except Exception as exc:
            log.error(
                "playlist push failed for %s (%s): %s",
                entity.get("id"),
                entity.get("name"),
                exc,
            )
            stats["push_failed"] += 1
            continue
        stats["pushed"] += 1

    log.info(
        "[fullsync] playlist sync project=%s fetched=%s pushed=%s "
        "fetch_failed=%s push_failed=%s zero_member_playlists=%s",
        project_name,
        stats["fetched"],
        stats["pushed"],
        stats["fetch_failed"],
        stats["push_failed"],
        stats["zero_members"],
    )
    return stats
