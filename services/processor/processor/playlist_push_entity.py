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


def build_playlist_push_entity(
    project_name: str,
    playlist: dict[str, Any],
    kitsu_folder_map: dict[str, str],
    ayon_base_url: str,
) -> dict[str, Any]:
    """Resolve members to AYON folder ids and build a ``Playlist`` /push entity."""
    label = (playlist.get("name") or "Playlist").strip() or "Playlist"
    ordered: list[str] = []
    for kid in ordered_kitsu_entity_ids_from_playlist(playlist):
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
    return {
        "type": "Playlist",
        "id": playlist["id"],
        "name": label,
        "ordered_ayon_folder_ids": ordered,
        "ayon_server_url": ayon_base_url,
    }


def sync_playlists_via_push_for_project(
    processor: "KitsuProcessor",
    kitsu_project_id: str,
    project_name: str,
    kitsu_folder_map: dict[str, str],
) -> None:
    """POST all Kitsu playlists for a project after structural folder sync."""
    if not playlist_sync_enabled(processor):
        return
    batch_size = 25
    batch: list[dict[str, Any]] = []
    base = ayon_api.get_base_url()
    n = 0
    for pl in iter_playlists_for_kitsu_project(kitsu_project_id):
        try:
            full = gazu.playlist.get_playlist(pl["id"])
        except Exception as exc:
            log.warning("playlist fetch %s: %s", pl.get("id"), exc)
            continue
        batch.append(
            build_playlist_push_entity(
                project_name, full, kitsu_folder_map, base
            )
        )
        if len(batch) >= batch_size:
            _post_playlist_batch(processor, project_name, batch)
            n += len(batch)
            batch = []
    if batch:
        _post_playlist_batch(processor, project_name, batch)
        n += len(batch)
    if n:
        log.info("[fullsync] pushed %s Kitsu playlist(s) as AYON lists", n)


def _post_playlist_batch(
    processor: "KitsuProcessor", project_name: str, entities: list[dict[str, Any]]
) -> None:
    response = ayon_api.post(
        f"{processor.entrypoint}/push",
        project_name=project_name,
        entities=entities,
    )
    response.raise_for_status()
