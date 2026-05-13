"""Section-scoped logging for content_sync (headers with Kitsu + AYON URLs once)."""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Mapping

import ayon_api
import gazu

from . import content_sync_browser_urls as _urls
from .utils import _ensure_gazu_host

log = logging.getLogger("content_sync")

_CTX: ContextVar["_ContentSyncLogSection | None"] = ContextVar(
    "content_sync_log_section",
    default=None,
)
# When a section ContextVar is not set (e.g. concept preview backfill), optional indent.
_LINE_PREFIX: ContextVar[str] = ContextVar("content_sync_log_line_prefix", default="")


@dataclass
class _ContentSyncLogSection:
    human_title: str
    kitsu_url: str | None
    ayon_url: str | None
    ayon_unresolved: bool
    child_prefix: str = "    "
    header_emitted: bool = False


def section_get() -> _ContentSyncLogSection | None:
    return _CTX.get()


def section_set(section: _ContentSyncLogSection) -> Token[_ContentSyncLogSection | None]:
    return _CTX.set(section)


def section_reset(token: Token[_ContentSyncLogSection | None]) -> None:
    _CTX.reset(token)


def line_prefix_set(prefix: str) -> Token[str]:
    return _LINE_PREFIX.set(prefix)


def line_prefix_reset(token: Token[str]) -> None:
    _LINE_PREFIX.reset(token)


def log_orphan_section_header(sec: _ContentSyncLogSection) -> None:
    """Emit the same three-line header without ContextVar (concept backfill, etc.)."""
    log.info("--- content_sync: %s", sec.human_title)
    if sec.kitsu_url:
        log.info("%sKitsu: %s", sec.child_prefix, sec.kitsu_url)
    if sec.ayon_unresolved or not sec.ayon_url:
        log.info("%sAYON: (unresolved)", sec.child_prefix)
    else:
        log.info("%sAYON: %s", sec.child_prefix, sec.ayon_url)


def section_emit_header_once() -> None:
    sec = _CTX.get()
    if not sec or sec.header_emitted:
        return
    sec.header_emitted = True
    log.info("--- content_sync: %s", sec.human_title)
    if sec.kitsu_url:
        log.info("%sKitsu: %s", sec.child_prefix, sec.kitsu_url)
    if sec.ayon_unresolved or not sec.ayon_url:
        log.info("%sAYON: (unresolved)", sec.child_prefix)
    else:
        log.info("%sAYON: %s", sec.child_prefix, sec.ayon_url)


def cs_log(level: int, msg: str, *args: Any) -> None:
    sec = _CTX.get()
    if sec:
        section_emit_header_once()
        prefix = sec.child_prefix
    else:
        prefix = _LINE_PREFIX.get()
    log.log(level, prefix + msg, *args)


def _folder_type_from_kitsu_entity_dict(ent: Mapping[str, Any] | None) -> str | None:
    if not ent:
        return None
    et = ent.get("entity_type")
    if isinstance(et, dict):
        n = et.get("name")
        if isinstance(n, str) and n.strip():
            return n.strip()
    t = ent.get("type")
    if isinstance(t, str) and t.strip():
        return t.strip()
    return None


def entity_row_folder_type(entity: Mapping[str, Any]) -> str | None:
    """Infer launcher folder_type from a Kitsu entity row (shot/asset/sequence…)."""
    t = entity.get("type")
    if isinstance(t, str) and t.strip():
        return t.strip()
    return _folder_type_from_kitsu_entity_dict(dict(entity))


def kitsu_entity_for_task_cached(
    task: Mapping[str, Any],
    cache: dict[str, dict | None],
) -> tuple[str | None, dict | None]:
    """Return (folder_type_name, entity_dict) for the task's Kitsu entity."""
    eid = task.get("entity_id")
    if not eid:
        return None, None
    sid = str(eid).strip()
    if not sid:
        return None, None
    if sid in cache:
        ent = cache[sid]
    else:
        _ensure_gazu_host()
        try:
            raw = gazu.entity.get_entity(sid)
            ent = raw if isinstance(raw, dict) else None
        except Exception:
            ent = None
        cache[sid] = ent
    return _folder_type_from_kitsu_entity_dict(ent), ent


def human_title_for_kitsu_task(task: Mapping[str, Any], entity: Mapping[str, Any] | None) -> str:
    tt = (
        task.get("task_type_name")
        or task.get("name")
        or str(task.get("task_type_id", ""))[:8]
        or "task"
    )
    en = ""
    if entity:
        en = str(entity.get("name") or entity.get("code") or "").strip()
    if not en:
        eid = task.get("entity_id")
        en = f"entity_id={str(eid)[:8]}…" if eid else "entity=?"
    kid = str(task.get("id", ""))[:8]
    return f"{en} / {tt} (kitsu_task={kid}…)"


def build_task_log_section(
    *,
    kitsu_api_server_url: str,
    kitsu_project_id: str,
    project_name: str,
    kitsu_task: Mapping[str, Any],
    ayon_task: Mapping[str, Any] | None,
    entity_cache: dict[str, dict | None],
) -> _ContentSyncLogSection:
    ft, ent = kitsu_entity_for_task_cached(kitsu_task, entity_cache)
    title = human_title_for_kitsu_task(kitsu_task, ent)
    tid = str(kitsu_task.get("id", "")).strip()
    kitsu_url = _urls.kitsu_browser_url(
        kitsu_api_server_url,
        kitsu_project_id,
        kitsu_task_id=tid or None,
        folder_type_for_entity=ft,
    )
    base = (ayon_api.get_base_url() or "").strip()
    pn = str(project_name or "").strip()
    ayon_url: str | None = None
    ayon_unresolved = not base or not pn
    if not ayon_unresolved and ayon_task:
        atid = str(ayon_task.get("id") or "").strip()
        fid = str(ayon_task.get("folderId") or ayon_task.get("folder_id") or "").strip()
        ayon_url = _urls.ayon_browser_url_task_context(
            base,
            pn,
            ayon_task_id=atid or None,
            ayon_folder_id=fid or None,
        )
    if not ayon_unresolved and not ayon_url:
        ayon_url = _urls.ayon_browser_url_project_overview(base, pn)
    return _ContentSyncLogSection(
        human_title=title,
        kitsu_url=kitsu_url,
        ayon_url=ayon_url,
        ayon_unresolved=ayon_unresolved,
    )


def build_preview_orphan_section(
    *,
    kitsu_api_server_url: str,
    kitsu_project_id: str,
    project_name: str,
    preview_file_id: str,
    resolved_kitsu_task_id: str,
    entity_id: str,
    kitsu_task: Mapping[str, Any] | None,
    ayon_task: Mapping[str, Any] | None,
    entity_cache: dict[str, dict | None],
) -> _ContentSyncLogSection:
    """Header for preview sync when not under ``_sync_all_task_content`` ContextVar."""
    if kitsu_task:
        return build_task_log_section(
            kitsu_api_server_url=kitsu_api_server_url,
            kitsu_project_id=kitsu_project_id,
            project_name=project_name,
            kitsu_task=kitsu_task,
            ayon_task=ayon_task,
            entity_cache=entity_cache,
        )
    ft: str | None = None
    ent: dict | None = None
    sid = str(entity_id or "").strip()
    if sid:
        if sid in entity_cache:
            ent = entity_cache[sid]
        else:
            _ensure_gazu_host()
            try:
                raw = gazu.entity.get_entity(sid)
                ent = raw if isinstance(raw, dict) else None
            except Exception:
                ent = None
            entity_cache[sid] = ent
        ft = _folder_type_from_kitsu_entity_dict(ent)
    if not ft:
        ft = "Asset"
    nm = ""
    if ent:
        nm = str(ent.get("name") or ent.get("code") or "").strip()
    pfx = str(preview_file_id)[:8]
    title = (
        f"Preview {pfx}… {nm or 'Kitsu entity'} "
        f"(surrogate kitsu_task={str(resolved_kitsu_task_id)[:8]}…)"
    )
    kitsu_url = _urls.kitsu_browser_url(
        kitsu_api_server_url,
        kitsu_project_id,
        kitsu_entity_id=sid or None,
        folder_type_for_entity=ft,
    )
    base = (ayon_api.get_base_url() or "").strip()
    pn = str(project_name or "").strip()
    ayon_url: str | None = None
    ayon_unresolved = not base or not pn
    if not ayon_unresolved and ayon_task:
        atid = str(ayon_task.get("id") or "").strip()
        fid = str(ayon_task.get("folderId") or ayon_task.get("folder_id") or "").strip()
        ayon_url = _urls.ayon_browser_url_task_context(
            base,
            pn,
            ayon_task_id=atid or None,
            ayon_folder_id=fid or None,
        )
    if not ayon_unresolved and not ayon_url:
        ayon_url = _urls.ayon_browser_url_project_overview(base, pn)
    return _ContentSyncLogSection(
        human_title=title,
        kitsu_url=kitsu_url,
        ayon_url=ayon_url,
        ayon_unresolved=ayon_unresolved,
    )


def build_entity_thumbnail_log_section(
    *,
    kitsu_api_server_url: str,
    kitsu_project_id: str,
    project_name: str,
    entity: Mapping[str, Any],
    folder_type_hint: str | None,
    ayon_folder: Mapping[str, Any] | None,
) -> _ContentSyncLogSection:
    eid = str(entity.get("id", "")).strip()
    nm = str(entity.get("name") or entity.get("code") or "").strip() or f"entity={eid[:8]}…"
    title = f"Thumbnail {nm} (kitsu_entity={eid[:8]}…)"
    ft = folder_type_hint or entity_row_folder_type(entity)
    kitsu_url = _urls.kitsu_browser_url(
        kitsu_api_server_url,
        kitsu_project_id,
        kitsu_entity_id=eid or None,
        folder_type_for_entity=ft,
    )
    base = (ayon_api.get_base_url() or "").strip()
    pn = str(project_name or "").strip()
    ayon_url: str | None = None
    ayon_unresolved = not base or not pn
    if not ayon_unresolved and ayon_folder:
        fid = str(ayon_folder.get("id") or "").strip()
        if fid:
            ayon_url = _urls.ayon_browser_url_workfiles(
                base, pn, ayon_folder_id=fid, ayon_task_id=None,
            )
    if not ayon_unresolved and not ayon_url:
        ayon_url = _urls.ayon_browser_url_project_overview(base, pn)
    return _ContentSyncLogSection(
        human_title=title,
        kitsu_url=kitsu_url,
        ayon_url=ayon_url,
        ayon_unresolved=ayon_unresolved,
    )
