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
        pl_id = str(playlist.get("id", "?"))[:8]
        log.debug(
            f"playlist {pl_id}: all_shots_for_playlist fallback failed: {exc}"
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
            pid = str(playlist.get("id", "?"))[:8]
            kid8 = kid[:8] if kid else "?"
            log.debug(
                f"playlist {pid}: skip member {kid8} "
                f"(no AYON folder with data.kitsuId)"
            )
    if not ordered:
        pl12 = str(playlist.get("id", "?"))[:12]
        log.warning(
            f"playlist {pl12} ({label[:64]!r}): zero AYON folder members resolved "
            f"(check Kitsu shots rows / sync and folder data.kitsuId)"
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
        url = getattr(response, "url", "")
        log.error(
            f"playlist push HTTP {response.status_code} {url} body={body!r}"
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
            f"[fullsync] playlist sync skipped project={project_name} "
            f"(playlist_sync.enabled=false)"
        )
        return stats

    base = ayon_api.get_base_url()
    for idx, pl in enumerate(iter_playlists_for_kitsu_project(kitsu_project_id), start=1):
        if idx == 1 or idx % 25 == 0:
            log.info(
                "[fullsync] playlist sync progress project=%s playlists_seen=%d",
                project_name,
                idx,
            )
        try:
            full = gazu.playlist.get_playlist(pl["id"])
        except Exception as exc:
            log.warning(f"playlist fetch {pl.get('id')}: {exc}")
            stats["fetch_failed"] += 1
            continue
        stats["fetched"] += 1
        entity = build_playlist_push_entity(
            project_name, full, kitsu_folder_map, base
        )
        if not entity.get("ordered_ayon_folder_ids"):
            stats["zero_members"] += 1
            if not _playlist_settings(processor).get("push_when_zero_members"):
                pl12 = str(full.get("id", "?"))[:12]
                log.info(
                    "playlist %s (%r): skipped push (no resolvable AYON folder members); "
                    "set sync_settings.playlist_sync.push_when_zero_members=true "
                    "to force POST anyway",
                    pl12,
                    (entity.get("name") or "")[:64],
                )
                continue
        try:
            _post_playlist_entities(processor, project_name, [entity])
        except Exception as exc:
            log.error(
                f"playlist push failed for {entity.get('id')} "
                f"({entity.get('name')}): {exc}"
            )
            stats["push_failed"] += 1
            continue
        stats["pushed"] += 1

    log.info(
        f"[fullsync] playlist sync project={project_name} "
        f"fetched={stats['fetched']} pushed={stats['pushed']} "
        f"fetch_failed={stats['fetch_failed']} push_failed={stats['push_failed']} "
        f"zero_member_playlists={stats['zero_members']}"
    )
    return stats
