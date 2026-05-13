"""Incremental content sync: Kitsu comments/previews/thumbnails -> AYON.

Used by the processor service for both Socket.IO-driven incremental sync
and full-sync content pass.  All heavy lifting (download, upload) happens
inline so the processor can run in a container with no local disk state.

AYON activity feed (versions vs comments):

- Kitsu preview sync creates or reuses **review** AYON versions. When
  ``impersonate_comment_authors`` is on and the Kitsu uploader maps to an AYON
  login, ``upload_reviewable``, preview-path ``upload_project_file``, and
  post-upload ``update_version`` merges for preview metadata / file ids run
  under ``as_username`` (same helper as comments). ``create_version`` /
  ``update_version`` for **version ``author``** (and correcting a processor
  placeholder author) use the same path when a Kitsu person resolves.

- There is **no** guarantee of a distinct publish/version activity per Kitsu
  preview or per every AYON Version in the project: one AYON version per
  Kitsu ``revision`` per review product (reused when revision matches); many
  early returns skip work entirely; duplicate ``preview_file_id`` skips
  before upload. Whether each ``upload_reviewable`` emits its own activity
  is defined by AYON server behavior, not this addon.

- Comment activities may pass Kitsu ``created_at`` as ``timestamp`` to
  ``create_activity``; the preview/version path does not set historical
  timestamps here.

- Kitsu ``previews`` on a comment are **not** AYON reviewables (those come from
  preview sync). A short ``Revision`` / ``review files`` markdown appendix is
  appended only when preview metadata is informative, unless
  ``append_kitsu_preview_manifest`` forces legacy behavior. Thin id-only
  previews do not create a misleading manifest; noise-only comments skip AYON
  activities.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator

import ayon_api
import gazu
import requests
from ayon_api.exceptions import HTTPRequestError
from nxtools import logging as nxtools_logging, slugify

from . import content_sync_browser_urls as browser_urls
from . import content_sync_logging as cs_log
from . import utils as processor_utils
from .checklist_subtask_sync import (
    bulk_sync_pinned_checklists_after_fullsync_enabled,
    delete_checklist_subtasks_for_comment,
    maybe_sync_checklist_subtasks_from_kitsu_comment,
)

if TYPE_CHECKING:
    from .processor import KitsuProcessor

log = logging.getLogger("content_sync")

_RETRYABLE_HTTP_STATUSES = frozenset((502, 503))
_RETRY_SLEEP_SEC = 1.5
_MAX_REVIEWABLE_LABEL_LEN = 120
# Must match ayon_server.activities.utils.MAX_BODY_LENGTH (ynput/ayon-backend).
_MAX_AYON_ACTIVITY_BODY_CHARS = 2000


def _comment_body_preview(comment: dict, *, max_len: int = 96) -> str:
    raw = comment.get("text")
    if not isinstance(raw, str):
        return ""
    one = " ".join(raw.split())
    if len(one) > max_len:
        return one[: max_len - 1] + "…"
    return one


def _enrich_kitsu_task_type_name(
    task: dict,
    task_types: dict[str, str],
) -> dict:
    """Shallow copy + ``task_type_name`` from ``task_types`` (avoids full ``preprocess_task``)."""
    tid = task.get("task_type_id")
    if tid and str(tid) in task_types and not task.get("task_type_name"):
        out = dict(task)
        out["task_type_name"] = task_types[str(tid)]
        return out
    return task


# Full-project content pass only: one get_folders / get_tasks per project per pass.
# Module global (not ContextVar): pytest and other hosts can duplicate execution
# contexts in ways that strand ContextVar values; a strict set/clear here matches
# the single-threaded processor fullsync + sequential test expectations.
_CONTENT_SYNC_AYON_LOOKUP_PASS: _AyonContentSyncLookupCache | None = None


class _AyonContentSyncLookupCache:
    """In-memory folder/task indexes for ``sync_all_content_for_project`` (pass-scoped)."""

    __slots__ = ("project_name", "_folder_by_kitsu", "_task_list", "_task_by_kitsu")

    def __init__(self, project_name: str) -> None:
        self.project_name = project_name
        self._folder_by_kitsu: dict[Any, dict] | None = None
        self._task_list: list[dict] | None = None
        self._task_by_kitsu: dict[Any, dict] | None = None

    def _ensure_folders(self) -> None:
        if self._folder_by_kitsu is not None:
            return
        kid_log = "cache-warm"
        idx: dict[Any, dict] = {}
        for attempt in range(2):
            try:
                for folder in ayon_api.get_folders(self.project_name):
                    k = (folder.get("data") or {}).get("kitsuId")
                    if k is None:
                        continue
                    if k not in idx:
                        idx[k] = folder
                self._folder_by_kitsu = idx
                return
            except (HTTPRequestError, requests.exceptions.RequestException) as exc:
                status = _http_error_status(exc)
                if (
                    attempt == 0
                    and status in _RETRYABLE_HTTP_STATUSES
                ):
                    log.warning(
                        "[content_sync] get_folders failed project=%s kitsu_entity=%s "
                        "http_status=%s; retrying once",
                        self.project_name,
                        kid_log,
                        status,
                    )
                    time.sleep(_RETRY_SLEEP_SEC)
                    continue
                log.warning(
                    "[content_sync] get_folders failed project=%s kitsu_entity=%s: %s",
                    self.project_name,
                    kid_log,
                    exc,
                )
                self._folder_by_kitsu = {}
                return
        self._folder_by_kitsu = {}

    def folder_by_kitsu_id(self, kitsu_id: str) -> dict | None:
        self._ensure_folders()
        assert self._folder_by_kitsu is not None
        return self._folder_by_kitsu.get(kitsu_id)

    def _ensure_tasks(self) -> None:
        if self._task_list is not None:
            return
        kid_log = "cache-warm"
        idx: dict[Any, dict] = {}
        for attempt in range(2):
            try:
                tasks = list(ayon_api.get_tasks(self.project_name))
                for task in tasks:
                    k = (task.get("data") or {}).get("kitsuId")
                    if k is None:
                        continue
                    if k not in idx:
                        idx[k] = task
                self._task_list = tasks
                self._task_by_kitsu = idx
                return
            except (HTTPRequestError, requests.exceptions.RequestException) as exc:
                status = _http_error_status(exc)
                if (
                    attempt == 0
                    and status in _RETRYABLE_HTTP_STATUSES
                ):
                    log.warning(
                        "[content_sync] get_tasks failed project=%s kitsu_task=%s "
                        "http_status=%s; retrying once",
                        self.project_name,
                        kid_log,
                        status,
                    )
                    time.sleep(_RETRY_SLEEP_SEC)
                    continue
                log.warning(
                    "[content_sync] get_tasks failed project=%s kitsu_task=%s: %s",
                    self.project_name,
                    kid_log,
                    exc,
                )
                self._task_list = []
                self._task_by_kitsu = {}
                return
        self._task_list = []
        self._task_by_kitsu = {}

    def task_by_kitsu_id(self, kitsu_id: str) -> dict | None:
        self._ensure_tasks()
        assert self._task_by_kitsu is not None
        return self._task_by_kitsu.get(kitsu_id)

    def vizdev_surrogate_task(
        self, folder_kitsu_id: str, surrogate_kitsu_id: str,
    ) -> dict | None:
        folder = self.folder_by_kitsu_id(folder_kitsu_id)
        if not folder:
            return None
        fid = folder.get("id")
        if not fid:
            return None
        self._ensure_tasks()
        assert self._task_list is not None
        for task in self._task_list:
            if _task_folder_id(task) != str(fid):
                continue
            tdata = task.get("data") or {}
            if tdata.get("kitsuId") == surrogate_kitsu_id:
                return task
            if (
                tdata.get("kitsuConceptId") == folder_kitsu_id
                and tdata.get("kitsuMirrorSlot") == "VizDev"
            ):
                return task
            if (
                tdata.get("kitsuLinkedEntityId") == folder_kitsu_id
                and tdata.get("kitsuMirrorSlot") == "VizDev"
            ):
                return task
        return None

    def iter_tasks(self) -> Iterator[dict]:
        self._ensure_tasks()
        return iter(self._task_list or [])


def _active_content_sync_ayon_cache(
    project_name: str,
) -> _AyonContentSyncLookupCache | None:
    c = _CONTENT_SYNC_AYON_LOOKUP_PASS
    if c is None or c.project_name != project_name:
        return None
    return c


@contextmanager
def _content_sync_ayon_lookup_cache_scope(project_name: str):
    """Activate AYON folder/task lookup cache for one full-project content sync pass."""
    global _CONTENT_SYNC_AYON_LOOKUP_PASS
    if _CONTENT_SYNC_AYON_LOOKUP_PASS is not None:
        log.warning(
            "[content_sync] pass lookup cache was still set; clearing before new scope",
        )
        _CONTENT_SYNC_AYON_LOOKUP_PASS = None
    _CONTENT_SYNC_AYON_LOOKUP_PASS = _AyonContentSyncLookupCache(project_name)
    try:
        yield
    finally:
        _CONTENT_SYNC_AYON_LOOKUP_PASS = None


# ---------------------------------------------------------------------------
# ID resolution helpers
# ---------------------------------------------------------------------------

def _http_error_status(exc: BaseException) -> int | None:
    if isinstance(exc, HTTPRequestError) and exc.response is not None:
        return getattr(exc.response, "status_code", None)
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return getattr(exc.response, "status_code", None)
    return None


def _http_error_body_snippet(exc: BaseException, max_len: int = 500) -> str:
    """Short server response text for logs (reviewable upload diagnostics)."""
    resp = None
    if isinstance(exc, HTTPRequestError) and exc.response is not None:
        resp = exc.response
    elif isinstance(exc, requests.exceptions.RequestException):
        resp = getattr(exc, "response", None)
    if resp is None:
        return ""
    try:
        text = (getattr(resp, "text", None) or "")[:max_len]
    except Exception:
        return ""
    return f" response_body={text!r}" if text else ""


def _http_error_detail_contains(exc: BaseException, needle: str) -> bool:
    """True if HTTP error response JSON/text contains needle (case-insensitive)."""
    resp = None
    if isinstance(exc, HTTPRequestError) and exc.response is not None:
        resp = exc.response
    elif isinstance(exc, requests.exceptions.HTTPError):
        resp = getattr(exc, "response", None)
    if resp is None:
        return False
    needle_l = needle.lower()
    try:
        j = resp.json()
        if isinstance(j, dict):
            for v in j.values():
                if isinstance(v, str) and needle_l in v.lower():
                    return True
    except Exception:
        pass
    try:
        return needle_l in (getattr(resp, "text", None) or "").lower()
    except Exception:
        return False


def _ayon_folder_by_kitsu_id(project_name: str, kitsu_id: str) -> dict | None:
    cache = _active_content_sync_ayon_cache(project_name)
    if cache is not None:
        return cache.folder_by_kitsu_id(kitsu_id)
    kid = (kitsu_id or "")[:8]
    for attempt in range(2):
        try:
            for folder in ayon_api.get_folders(project_name):
                if (folder.get("data") or {}).get("kitsuId") == kitsu_id:
                    return folder
            return None
        except (HTTPRequestError, requests.exceptions.RequestException) as exc:
            status = _http_error_status(exc)
            if (
                attempt == 0
                and status in _RETRYABLE_HTTP_STATUSES
            ):
                log.warning(
                    "[content_sync] get_folders failed project=%s kitsu_entity=%s "
                    "http_status=%s; retrying once",
                    project_name,
                    kid,
                    status,
                )
                time.sleep(_RETRY_SLEEP_SEC)
                continue
            log.warning(
                "[content_sync] get_folders failed project=%s kitsu_entity=%s: %s",
                project_name,
                kid,
                exc,
            )
            return None
    return None


def concept_vizdev_surrogate_kitsu_id(concept_id: str) -> str:
    """Deterministic kitsuId for the AYON-only VizDev task (must match server)."""
    return f"kitsu:concept:{concept_id}:vizdev"


def concept_vizdev_surrogate_for_linked_entity(linked_entity_id: str) -> str:
    """Must match ``server/kitsu/concept_utils.concept_vizdev_surrogate_for_linked_entity``."""
    return f"kitsu:link:{linked_entity_id}:vizdev"


def concept_vizdev_surrogate_unlinked_pool() -> str:
    """Must match ``server/kitsu/concept_utils.concept_vizdev_surrogate_unlinked_pool``."""
    return "kitsu:concepts:unlinked_pool:vizdev"


def _concept_id_from_vizdev_surrogate_task_id(task_id: str) -> str | None:
    prefix = "kitsu:concept:"
    suffix = ":vizdev"
    if not (task_id or "").strip():
        return None
    if not task_id.startswith(prefix) or not task_id.endswith(suffix):
        return None
    return task_id[len(prefix): -len(suffix)]


def _linked_entity_id_from_vizdev_surrogate_task_id(task_id: str) -> str | None:
    prefix = "kitsu:link:"
    suffix = ":vizdev"
    if not (task_id or "").strip():
        return None
    if not task_id.startswith(prefix) or not task_id.endswith(suffix):
        return None
    return task_id[len(prefix): -len(suffix)]


def _task_folder_id(task: dict) -> str | None:
    fid = task.get("folderId") or task.get("folder_id")
    return str(fid) if fid else None


def _ayon_vizdev_task_for_concept(
    project_name: str, concept_kitsu_id: str,
) -> dict | None:
    """Resolve the VizDev child task (legacy: folder ``kitsuId`` == Kitsu concept id)."""
    surrogate = concept_vizdev_surrogate_kitsu_id(concept_kitsu_id)
    return _ayon_vizdev_task_by_surrogate(
        project_name, concept_kitsu_id, surrogate,
    )


def _ayon_vizdev_task_by_surrogate(
    project_name: str,
    folder_kitsu_id: str,
    surrogate_kitsu_id: str,
) -> dict | None:
    """Resolve VizDev task under the Concept folder matching ``surrogate_kitsuId``."""
    cache = _active_content_sync_ayon_cache(project_name)
    if cache is not None:
        return cache.vizdev_surrogate_task(folder_kitsu_id, surrogate_kitsu_id)
    folder = _ayon_folder_by_kitsu_id(project_name, folder_kitsu_id)
    if not folder:
        return None
    fid = folder.get("id")
    if not fid:
        return None
    for attempt in range(2):
        try:
            for task in ayon_api.get_tasks(project_name):
                if _task_folder_id(task) != str(fid):
                    continue
                tdata = task.get("data") or {}
                if tdata.get("kitsuId") == surrogate_kitsu_id:
                    return task
                if (
                    tdata.get("kitsuConceptId") == folder_kitsu_id
                    and tdata.get("kitsuMirrorSlot") == "VizDev"
                ):
                    return task
                if (
                    tdata.get("kitsuLinkedEntityId") == folder_kitsu_id
                    and tdata.get("kitsuMirrorSlot") == "VizDev"
                ):
                    return task
            return None
        except (HTTPRequestError, requests.exceptions.RequestException) as exc:
            status = _http_error_status(exc)
            if (
                attempt == 0
                and status in _RETRYABLE_HTTP_STATUSES
            ):
                log.warning(
                    "[content_sync] get_tasks failed project=%s folder_kitsu=%s "
                    "http_status=%s; retrying once",
                    project_name,
                    (folder_kitsu_id or "")[:8],
                    status,
                )
                time.sleep(_RETRY_SLEEP_SEC)
                continue
            log.warning(
                "[content_sync] get_tasks failed project=%s folder_kitsu=%s: %s",
                project_name,
                (folder_kitsu_id or "")[:8],
                exc,
            )
            return None
    return None


def _kitsu_task_entity_looks_like_concept(task: dict) -> bool:
    ent = task.get("entity") or {}
    if isinstance(ent, dict):
        t = (ent.get("type") or ent.get("type_name") or "").strip()
        if t.lower() == "concept":
            return True
    for key in ("entity_type_name", "entity_type"):
        v = task.get(key)
        if isinstance(v, str) and v.lower() == "concept":
            return True
        if isinstance(v, dict) and str(v.get("name", "")).lower() == "concept":
            return True
    return False


def _ayon_task_by_kitsu_id(project_name: str, kitsu_id: str) -> dict | None:
    cache = _active_content_sync_ayon_cache(project_name)
    if cache is not None:
        return cache.task_by_kitsu_id(kitsu_id)
    kid = (kitsu_id or "")[:8]
    for attempt in range(2):
        try:
            for task in ayon_api.get_tasks(project_name):
                if (task.get("data") or {}).get("kitsuId") == kitsu_id:
                    return task
            return None
        except (HTTPRequestError, requests.exceptions.RequestException) as exc:
            status = _http_error_status(exc)
            if (
                attempt == 0
                and status in _RETRYABLE_HTTP_STATUSES
            ):
                log.warning(
                    "[content_sync] get_tasks failed project=%s kitsu_task=%s "
                    "http_status=%s; retrying once",
                    project_name,
                    kid,
                    status,
                )
                time.sleep(_RETRY_SLEEP_SEC)
                continue
            log.warning(
                "[content_sync] get_tasks failed project=%s kitsu_task=%s: %s",
                project_name,
                kid,
                exc,
            )
            return None
    return None


def _reviewable_label(original_name: str, preview_file_id: str) -> str:
    """Safe stem for AYON reviewable filenames (alphanumeric + underscore); preview id suffix."""
    raw = (original_name or "").strip()
    if not raw or raw in (".", ".."):
        stem = "preview"
    else:
        stem = os.path.basename(raw.replace("\\", "/"))
    safe = re.sub(r"[^A-Za-z0-9]+", "_", stem)
    safe = re.sub(r"_+", "_", safe).strip("_") or "preview"
    pid = str(preview_file_id).replace("-", "")[:12]
    suffix = f"_{pid}" if pid else ""
    max_stem = max(8, _MAX_REVIEWABLE_LABEL_LEN - len(suffix))
    if len(safe) > max_stem:
        safe = safe[:max_stem]
    out = f"{safe}{suffix}"
    if len(out) > _MAX_REVIEWABLE_LABEL_LEN:
        out = out[:_MAX_REVIEWABLE_LABEL_LEN]
    return out


def _reviewable_upload_basename(original_name: str, preview_file_id: str, ext: str) -> str:
    """Basename for X-File-Name on reviewable upload (stem from Kitsu + sanitized extension)."""
    stem = _reviewable_label(original_name, preview_file_id)
    e = (ext or "bin").lstrip(".").lower()
    e = re.sub(r"[^a-z0-9]", "", e) or "bin"
    return f"{stem}.{e}"


def _preview_sidecar_base(position: int, preview_file_id: str) -> str:
    """Filename stem for JSON sidecars; id without hyphens/specials for strict servers."""
    pid = re.sub(r"[^A-Za-z0-9]", "", str(preview_file_id))[:32]
    if not pid:
        pid = "unknown"
    return f"{int(position):02d}_{pid}"


def _preview_download_url(preview: dict) -> str:
    ext = preview.get("extension", "png")
    pid = preview["id"]
    prefix = "movies" if ext == "mp4" else "pictures"
    return f"{prefix}/originals/preview-files/{pid}.{ext}"


_REVIEWABLE_IMAGE_EXT = frozenset({
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff",
})
_REVIEWABLE_VIDEO_EXT = frozenset({
    "mp4", "mov", "m4v", "webm", "avi", "mkv", "mpg", "mpeg",
})


def _mime_type_for_reviewable_extension(ext: str) -> str | None:
    e = (ext or "").lstrip(".").lower()
    if e in ("jpg", "jpeg"):
        return "image/jpeg"
    if e in ("tif", "tiff"):
        return "image/tiff"
    if e in _REVIEWABLE_IMAGE_EXT:
        return f"image/{e}"
    if e == "mov":
        return "video/quicktime"
    if e in ("mpg", "mpeg"):
        return "video/mpeg"
    if e == "m4v":
        return "video/x-m4v"
    if e in _REVIEWABLE_VIDEO_EXT:
        return f"video/{e}"
    return None


def _extension_from_original_name(original_name: str) -> str | None:
    raw = (original_name or "").strip()
    if not raw:
        return None
    base = os.path.basename(raw.replace("\\", "/"))
    _stem, dot, suf = base.rpartition(".")
    if not dot or not suf:
        return None
    e = suf.lower().lstrip(".")
    e = re.sub(r"[^a-z0-9]", "", e)
    if e in _REVIEWABLE_IMAGE_EXT | _REVIEWABLE_VIDEO_EXT:
        return e
    return None


def _sniff_reviewable_media(tmp_path: str) -> tuple[str | None, str | None]:
    """Detect image/video kind from file header; returns (extension_without_dot, mime)."""
    try:
        with open(tmp_path, "rb") as fh:
            head = fh.read(32)
    except OSError:
        return None, None
    if len(head) < 12:
        return None, None
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "gif", "image/gif"
    if head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WEBP":
        return "webp", "image/webp"
    if head.startswith(b"BM"):
        return "bmp", "image/bmp"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand == b"qt  ":
            return "mov", "video/quicktime"
        return "mp4", "video/mp4"
    return None, None


def _infer_reviewable_basename_and_mime(
    tmp_path: str,
    preview: dict,
    original_name: str,
    preview_file_id: str,
    kitsu_extension: str,
) -> tuple[str | None, str | None]:
    """Basename (with ext) and Content-Type for upload_reviewable, or (None, None) if unsupported."""
    ext_sniff, mime_sniff = _sniff_reviewable_media(tmp_path)
    if ext_sniff and mime_sniff:
        return (
            _reviewable_upload_basename(
                original_name, preview_file_id, ext_sniff,
            ),
            mime_sniff,
        )
    ext_name = _extension_from_original_name(original_name)
    if ext_name:
        mime = _mime_type_for_reviewable_extension(ext_name)
        if mime:
            return (
                _reviewable_upload_basename(
                    original_name, preview_file_id, ext_name,
                ),
                mime,
            )
    ke = str(kitsu_extension or "bin").lstrip(".").lower()
    ke = re.sub(r"[^a-z0-9]", "", ke) or "bin"
    if ke not in ("bin",) and ke in (_REVIEWABLE_IMAGE_EXT | _REVIEWABLE_VIDEO_EXT):
        mime = _mime_type_for_reviewable_extension(ke)
        if mime:
            return (
                _reviewable_upload_basename(
                    original_name, preview_file_id, ke,
                ),
                mime,
            )
    return None, None


def _is_pdf_preview(tmp_path: str, kitsu_extension: str) -> bool:
    """True if Kitsu extension is pdf or file begins with %PDF- (after optional whitespace)."""
    e = str(kitsu_extension or "").lstrip(".").lower()
    e = re.sub(r"[^a-z0-9]", "", e)
    if e == "pdf":
        return True
    try:
        with open(tmp_path, "rb") as fh:
            chunk = fh.read(4096)
    except OSError:
        return False
    stripped = chunk.lstrip(b" \t\r\n\x00")
    return stripped.startswith(b"%PDF-")


def _merge_kitsu_preview_metadata_on_version(
    project_name: str,
    version_id: str,
    preview_file_id: str,
    annotations: Any,
    preview_file: dict[str, Any],
    *,
    project_file_id: str | None = None,
    preview_kind: str | None = None,
) -> None:
    """Store Kitsu preview annotations + raw preview record on version.data (not reviewables)."""
    ver = ayon_api.get_version_by_id(project_name, version_id)
    if not ver:
        log.warning(
            "get_version_by_id missing for %s; skip kitsuPreviewArtifacts merge",
            version_id,
        )
        return
    data = dict(ver.get("data") or {})
    art: dict[str, Any] = dict(data.get("kitsuPreviewArtifacts") or {})
    pid = str(preview_file_id)
    entry: dict[str, Any] = {
        "annotations": annotations,
        "previewFile": preview_file,
    }
    if project_file_id:
        entry["projectFileId"] = project_file_id
    if preview_kind:
        entry["previewKind"] = preview_kind
    art[pid] = entry
    data["kitsuPreviewArtifacts"] = art
    try:
        ayon_api.update_version(project_name, version_id, data=data)
    except Exception as exc:
        log.warning(
            "update_version kitsuPreviewArtifacts failed for %s: %s",
            version_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Thumbnail sync
# ---------------------------------------------------------------------------

def _kitsu_png_thumbnail_relative_url(preview_file_id: str) -> str:
    """Kitsu-generated PNG tile URL (AYON ``create_thumbnail`` rejects some originals)."""
    return f"pictures/thumbnails/preview-files/{preview_file_id}.png"


def sync_thumbnail_to_ayon(
    processor: "KitsuProcessor",
    entity: dict,
    project_name: str,
    *,
    kitsu_project_id: str | None = None,
):
    """Download Kitsu entity preview (original image when possible) and set AYON folder thumbnail."""
    preview_file_id = entity.get("preview_file_id")
    if not preview_file_id:
        return

    kitsu_id = entity["id"]
    ayon_folder = _ayon_folder_by_kitsu_id(project_name, kitsu_id)
    if not ayon_folder:
        return

    thumb_tok = None
    if kitsu_project_id:
        ft_hint = cs_log.entity_row_folder_type(entity)
        sec = cs_log.build_entity_thumbnail_log_section(
            kitsu_api_server_url=processor.kitsu_server_url,
            kitsu_project_id=kitsu_project_id,
            project_name=project_name,
            entity=entity,
            folder_type_hint=ft_hint,
            ayon_folder=ayon_folder,
        )
        thumb_tok = cs_log.section_set(sec)

    tmp_path: str | None = None
    try:
        existing_thumb_kid = (ayon_folder.get("data") or {}).get("kitsuThumbnailPreviewId")
        if str(existing_thumb_kid) == str(preview_file_id):
            log.debug(
                "[content_sync] skip folder thumbnail: already set to this Kitsu preview "
                "(folder_id=%s preview_file_id=%s)",
                ayon_folder.get("id"),
                preview_file_id,
            )
            return

        try:
            pf = gazu.files.get_preview_file(preview_file_id)
        except Exception as exc:
            cs_log.cs_log(
                logging.WARNING,
                "get_preview_file failed for entity %s: %s",
                kitsu_id,
                exc,
            )
            return

        if not pf:
            return

        st = (pf.get("status") or "").lower()
        if st and st != "ready":
            log.debug(
                "Entity preview %s not ready (%s), skipping thumbnail sync",
                preview_file_id,
                st,
            )
            return

        # Always use Kitsu's PNG thumbnail derivative. Uploading WebP/HEIF/tiff or
        # mis-tagged originals caused 415 Unsupported Media Type on AYON thumbnail API.
        url = _kitsu_png_thumbnail_relative_url(str(preview_file_id))
        suffix = ".png"

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name

        try:
            gazu.client.download(url, tmp_path)
            thumb_id = ayon_api.create_thumbnail(project_name, tmp_path)
            ayon_api.update_folder(
                project_name, ayon_folder["id"],
                thumbnail_id=thumb_id,
                data={
                    **(ayon_folder.get("data") or {}),
                    "kitsuThumbnailPreviewId": preview_file_id,
                },
            )
            cs_log.cs_log(
                logging.INFO,
                "Thumbnail synced for entity %s -> folder %s",
                kitsu_id,
                ayon_folder["id"],
            )
        except Exception as exc:
            cs_log.cs_log(
                logging.WARNING,
                "Thumbnail sync failed for %s: %s",
                kitsu_id,
                exc,
            )
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)
    finally:
        if thumb_tok is not None:
            cs_log.section_reset(thumb_tok)


# ---------------------------------------------------------------------------
# Review / preview sync
# ---------------------------------------------------------------------------

def _preview_source_concept_id_from_payload(preview: dict) -> str | None:
    """Best-effort Kitsu concept id when preview payload carries entity/source ids."""
    for key in ("concept_id", "entity_id", "source_id"):
        v = preview.get(key)
        if isinstance(v, dict):
            v = v.get("id")
        if v:
            s = str(v).strip()
            if s:
                return s
    return None


def _per_linked_concept_review_product_name(
    task_type_name: str,
    *,
    source_kitsu_concept_id: str | None,
    preview_file_id: str,
) -> str:
    """One review product per contributing Kitsu concept under a merged link folder."""
    base = f"{(task_type_name or 'unknown').lower()}KitsuReview"
    cid = (source_kitsu_concept_id or "").strip()
    if cid:
        suf = slugify(cid, separator="_")[:48] or "src"
        name = f"{base}_c_{suf}"
    else:
        pf = str(preview_file_id).replace("-", "_")
        name = f"{base}_pf_{pf[:24]}"
    if len(name) > 120:
        name = name[:120]
    return name


def _find_or_create_review_product(
    project_name: str,
    folder_id: str,
    task_type_name: str,
    kitsu_task_id: str,
    *,
    product_name: str | None = None,
    product_data_extra: dict[str, Any] | None = None,
) -> str:
    pname = product_name or f"{task_type_name.lower()}KitsuReview"
    for prod in ayon_api.get_products(project_name, folder_ids=[folder_id]):
        if prod["name"] == pname:
            return prod["id"]
    pdata: dict[str, Any] = {"kitsuTaskId": kitsu_task_id}
    if product_data_extra:
        pdata.update(product_data_extra)
    return ayon_api.create_product(
        project_name,
        name=pname,
        product_type="review",
        folder_id=folder_id,
        data=pdata,
    )


def _is_processor_placeholder_version_author(author: object) -> bool:
    """True if AYON version author looks like the kitsu-processor service identity."""
    if not author or not isinstance(author, str):
        return False
    a = author.strip().lower()
    return a == "kitsu-processor" or a.startswith("kitsu-processor-")


def _person_id_from_kitsu_preview(preview: dict) -> str | None:
    pid = preview.get("person_id")
    if isinstance(pid, dict):
        pid = pid.get("id")
    if pid:
        s = str(pid).strip()
        return s or None
    person = preview.get("person")
    if isinstance(person, dict) and person.get("id"):
        s = str(person["id"]).strip()
        return s or None
    return None


def _kitsu_person_dict_for_preview_uploader(
    preview: dict, kitsu_comment_id: str,
) -> dict[str, Any]:
    """Kitsu person who uploaded the preview, or comment author when tied to a comment."""
    pid = _person_id_from_kitsu_preview(preview)
    cid = (kitsu_comment_id or "").strip()
    if not pid and cid:
        try:
            c = gazu.task.get_comment(cid)
            if isinstance(c, dict):
                cp = c.get("person_id")
                if isinstance(cp, dict):
                    cp = cp.get("id")
                if cp:
                    pid = str(cp).strip() or None
        except Exception as exc:
            log.debug("get_comment for preview author %s: %s", cid[:8], exc)
    if not pid:
        return {}
    try:
        p = gazu.person.get_person(pid)
        return p if isinstance(p, dict) else {}
    except Exception as exc:
        log.debug("get_person for preview author %s: %s", pid[:8], exc)
        return {}


def _find_review_version_id_for_revision(
    project_name: str, product_id: str, revision: int,
) -> str | None:
    for ver in ayon_api.get_versions(project_name, product_ids=[product_id]):
        if ver.get("version") == revision:
            return str(ver["id"])
    return None


def _create_review_version_entity(
    project_name: str,
    product_id: str,
    revision: int,
    kitsu_task_id: str,
    kitsu_comment_id: str,
    ayon_task_id: str | None,
    author_login: str | None,
) -> str:
    data = {
        "kitsuRevision": revision,
        "kitsuCommentId": kitsu_comment_id,
        "kitsuTaskId": kitsu_task_id,
    }
    kwargs: dict[str, Any] = {
        "project_name": project_name,
        "version": revision,
        "product_id": product_id,
        "task_id": ayon_task_id,
        "data": data,
    }
    if author_login and "author" in inspect.signature(ayon_api.create_version).parameters:
        kwargs["author"] = author_login
    try:
        return ayon_api.create_version(**kwargs)
    except TypeError:
        kwargs.pop("author", None)
        return ayon_api.create_version(**kwargs)


def ayon_update_version_supports_author_parameter() -> bool:
    """True when ``ayon_api.update_version`` accepts ``author=`` (required for version author repair)."""
    try:
        return "author" in inspect.signature(ayon_api.update_version).parameters
    except (TypeError, ValueError, AttributeError):
        return False


def log_ayon_version_author_update_capability() -> None:
    """Log once at processor startup if author PATCH is unavailable (older ayon-python-api)."""
    if ayon_update_version_supports_author_parameter():
        return
    log.warning(
        "ayon_api.update_version has no 'author' parameter; review version author "
        "repair and placeholder correction cannot PATCH author. Upgrade ayon-python-api."
    )


def _update_version_author_if_supported(
    project_name: str, version_id: str, author_login: str,
) -> None:
    if not ayon_update_version_supports_author_parameter():
        return
    try:
        ayon_api.update_version(project_name, version_id, author=author_login)
    except TypeError:
        pass


def _ensure_review_version_author_with_impersonation(
    processor: "KitsuProcessor",
    project_name: str,
    version_id: str,
    ver_snapshot: dict[str, Any] | None,
    kitsu_person: dict[str, Any],
    email_cache: dict[str, str],
    ayon_login: str | None,
) -> None:
    """Patch version ``author`` when still on processor placeholder; prefer ``as_username``."""
    if not ver_snapshot or not ayon_login:
        return
    if not _is_processor_placeholder_version_author(ver_snapshot.get("author")):
        return

    def _patch() -> None:
        _update_version_author_if_supported(project_name, version_id, ayon_login)

    if kitsu_person:
        _run_ayon_as_kitsu_person_when_service(
            processor,
            project_name,
            kitsu_person,
            email_cache,
            _patch,
            acl_log_label="Review version author",
        )
    else:
        _patch()


def _preview_file_id_in_version_data(preview_file_id: str, version_dict: dict | None) -> bool:
    """True if Kitsu preview file id is already listed on version.data.kitsuPreviewFileIds."""
    if not version_dict:
        return False
    data = version_dict.get("data") or {}
    kid_list = data.get("kitsuPreviewFileIds") or []
    if not isinstance(kid_list, list):
        return False
    needle = str(preview_file_id)
    return any(str(x) == needle for x in kid_list)


def _activity_kitsu_comment_id(activity: dict) -> str | None:
    """Resolve kitsuCommentId whether API exposes activityData or data (AYON version differences)."""
    payload = activity.get("activityData") or activity.get("data") or {}
    cid = payload.get("kitsuCommentId")
    return str(cid) if cid else None


def _activity_kitsu_payload(activity: dict) -> dict[str, Any]:
    raw = activity.get("activityData") or activity.get("data") or {}
    return raw if isinstance(raw, dict) else {}


def _comment_part_header_line(part: int, total: int) -> str:
    if total <= 1:
        return ""
    return f"_(Part {part}/{total})_\n\n"


def _sha256_utf8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _try_split_full_body_into_n_parts(full_body: str, n: int, max_chars: int) -> list[str] | None:
    """Split full_body into n activity bodies, each <= max_chars (with Part i/n header when n > 1)."""
    if n < 1:
        return None
    if n == 1:
        return [full_body] if len(full_body) <= max_chars else None
    out: list[str] = []
    pos = 0
    total_len = len(full_body)
    for i in range(1, n + 1):
        header = _comment_part_header_line(i, n)
        budget = max_chars - len(header)
        if budget < 1:
            return None
        remaining = total_len - pos
        parts_left = n - i + 1
        if i == n:
            if remaining > budget:
                return None
            out.append(header + full_body[pos:])
            return out
        min_reserve = parts_left - 1
        take = min(budget, remaining - min_reserve)
        if take < 1:
            return None
        out.append(header + full_body[pos : pos + take])
        pos += take
    return None


def _split_comment_body_for_ayon_activities(full_body: str) -> list[str]:
    """One string per AYON comment activity; each respects ``_MAX_AYON_ACTIVITY_BODY_CHARS``."""
    max_c = _MAX_AYON_ACTIVITY_BODY_CHARS
    if len(full_body) <= max_c:
        return [full_body]
    total_len = len(full_body)
    for n in range(2, total_len + 1):
        attempt = _try_split_full_body_into_n_parts(full_body, n, max_c)
        if attempt is not None:
            return attempt
    return [full_body[:max_c]]


def _activities_for_kitsu_comment_id(
    activities: list[dict], kitsu_comment_id: str,
) -> list[dict]:
    kid = str(kitsu_comment_id)
    return [a for a in activities if _activity_kitsu_comment_id(a) == kid]


def _existing_comment_sync_uptodate(
    matches: list[dict],
    part_count: int,
    body_sha256: str,
    full_body: str,
) -> bool:
    """True when AYON already reflects this Kitsu comment (multipart + hash, or legacy single)."""
    if len(matches) != part_count:
        return False
    by_part: dict[int, dict] = {}
    for act in matches:
        payload = _activity_kitsu_payload(act)
        p_raw = payload.get("kitsuCommentPart")
        pc_raw = payload.get("kitsuCommentPartCount")
        if p_raw is None and pc_raw is None:
            part = 1
            p_count = 1
        else:
            try:
                part = int(p_raw) if p_raw is not None else 1
                p_count = int(pc_raw) if pc_raw is not None else part_count
            except (TypeError, ValueError):
                return False
        if p_count != part_count:
            return False
        stored_sha = payload.get("kitsuCommentBodySha256")
        if stored_sha != body_sha256:
            if part_count == 1 and len(matches) == 1 and stored_sha is None:
                act_body = act.get("body") if isinstance(act.get("body"), str) else ""
                if act_body == full_body:
                    return True
            return False
        if part in by_part:
            return False
        by_part[part] = act
    return all(i in by_part for i in range(1, part_count + 1))


def _delete_all_activities_for_kitsu_comment(
    project_name: str,
    ayon_task_id: str,
    kitsu_comment_id: str,
    *,
    activities: list[dict] | None = None,
) -> None:
    if activities is None:
        src = list(
            ayon_api.get_activities(
                project_name,
                entity_ids=[ayon_task_id],
                activity_types=["comment"],
            ),
        )
    else:
        src = activities
    for act in src:
        if _activity_kitsu_comment_id(act) != str(kitsu_comment_id):
            continue
        try:
            ayon_api.delete_activity(project_name, act["activityId"])
            cs_log.cs_log(
                logging.INFO,
                "Deleted activity %s for comment %s",
                act.get("activityId"),
                kitsu_comment_id[:8],
            )
        except Exception as exc:
            cs_log.cs_log(
                logging.ERROR,
                "Failed to delete activity for comment %s: %s",
                kitsu_comment_id[:8],
                exc,
            )


def _merge_kitsu_preview_file_ids_on_version(
    project_name: str, version_id: str, new_ids: list[str],
) -> None:
    """Append any new Kitsu preview file ids into version.data.kitsuPreviewFileIds."""
    if not new_ids:
        return
    ver = ayon_api.get_version_by_id(project_name, version_id)
    if not ver:
        log.warning("get_version_by_id missing for %s; skip kitsuPreviewFileIds merge", version_id)
        return
    data = dict(ver.get("data") or {})
    existing = list(data.get("kitsuPreviewFileIds") or [])
    existing_norm = {str(x) for x in existing}
    for nid in new_ids:
        if str(nid) not in existing_norm:
            existing.append(nid)
            existing_norm.add(str(nid))
    data["kitsuPreviewFileIds"] = existing
    try:
        ayon_api.update_version(project_name, version_id, data=data)
    except Exception as exc:
        log.warning("update_version kitsuPreviewFileIds failed for %s: %s", version_id, exc)


def sync_preview_to_ayon(
    processor: "KitsuProcessor",
    preview_file_id: str,
    task_id: str,
    project_id: str,
    *,
    source_kitsu_concept_id: str | None = None,
):
    """Download a single Kitsu preview file and upload as AYON reviewable.

    For ``per_linked_entity`` concept folders (VizDev surrogate ``kitsu:link:…``),
    pass ``source_kitsu_concept_id`` so each contributing Kitsu concept gets its
    own review product under the merged AYON folder; otherwise all previews share
    one product and collide on revision / kitsuPreviewFileIds.

    See module docstring: ``upload_reviewable`` / preview ``upload_project_file``
    and post-upload version metadata merges run under ``as_username`` when
    impersonation is on and the Kitsu uploader maps to an AYON login.
    """
    project_name = processor.get_paired_ayon_project(project_id)
    if not project_name:
        return

    _orphan_line_tok = None
    _pe_cache: dict[str, dict | None] = {}

    try:
        processor_utils.set_kitsu_host(processor.kitsu_server_url)

        try:
            preview = gazu.files.get_preview_file(preview_file_id)
        except Exception as exc:
            log.error("Failed to get preview file %s: %s", preview_file_id, exc)
            return

        if not preview:
            return

        resolved_task_id = (task_id or "").strip()
        if not resolved_task_id:
            tid = preview.get("task_id")
            if isinstance(tid, dict):
                tid = tid.get("id")
            if tid:
                resolved_task_id = str(tid)

        pool_surrogate = concept_vizdev_surrogate_unlinked_pool()
        is_pool_surrogate = resolved_task_id == pool_surrogate

        concept_from_surrogate = (
            None
            if is_pool_surrogate
            else _concept_id_from_vizdev_surrogate_task_id(resolved_task_id)
        )
        link_from_surrogate = (
            None
            if is_pool_surrogate
            else _linked_entity_id_from_vizdev_surrogate_task_id(resolved_task_id)
        )
        task: dict | None = None
        entity_id = ""
        task_type = "unknown"

        if is_pool_surrogate:
            _cz = (processor.settings.get("sync_settings") or {}).get("concept_sync")
            cs = processor_utils.normalize_concept_sync_dict(
                _cz if isinstance(_cz, dict) else None,
            )
            anchor = (cs or {}).get("unlinked_concepts_project_kitsu_id")
            anchor = (
                str(anchor).strip() if anchor else ""
            ) or processor_utils.DEFAULT_UNLINKED_PROJECT_KITSU_ID
            entity_id = anchor
            task_type = "VizDev"
        elif concept_from_surrogate:
            entity_id = concept_from_surrogate
            task_type = "VizDev"
        elif link_from_surrogate:
            entity_id = link_from_surrogate
            task_type = "VizDev"
        else:
            if not resolved_task_id:
                log.debug(
                    "[content_sync] preview %s missing task_id (and preview has none)",
                    preview_file_id,
                )
                return
            try:
                task = gazu.task.get_task(resolved_task_id)
            except Exception as exc:
                cs_log.cs_log(
                    logging.ERROR,
                    "Failed to get Kitsu task %s: %s",
                    resolved_task_id,
                    exc,
                )
                return
            entity_id = str(task.get("entity_id") or "")
            task_type = str(
                task.get("task_type_name")
                or task.get("task_type_id")
                or "unknown",
            )

        ayon_folder = _ayon_folder_by_kitsu_id(project_name, entity_id)
        if not ayon_folder:
            log.debug("No AYON folder for entity %s", (entity_id or "")[:8])
            return

        is_concept_media_path = (
            bool(concept_from_surrogate)
            or bool(link_from_surrogate)
            or is_pool_surrogate
            or (task is not None and _kitsu_task_entity_looks_like_concept(task))
        )
        if is_concept_media_path:
            task_type = "VizDev"

        ayon_task = _ayon_task_by_kitsu_id(project_name, resolved_task_id)
        if ayon_task is None and entity_id and is_concept_media_path:
            ayon_task = _ayon_vizdev_task_by_surrogate(
                project_name, entity_id, resolved_task_id,
            )

        def _ensure_preview_orphan_ctx() -> None:
            nonlocal _orphan_line_tok
            if cs_log.section_get() is not None or _orphan_line_tok is not None:
                return
            sec = cs_log.build_preview_orphan_section(
                kitsu_api_server_url=processor.kitsu_server_url,
                kitsu_project_id=project_id,
                project_name=project_name,
                preview_file_id=str(preview_file_id),
                resolved_kitsu_task_id=str(resolved_task_id),
                entity_id=str(entity_id),
                kitsu_task=task,
                ayon_task=ayon_task,
                entity_cache=_pe_cache,
            )
            cs_log.log_orphan_section_header(sec)
            _orphan_line_tok = cs_log.line_prefix_set("    ")

        if is_concept_media_path and ayon_task is None:
            _ensure_preview_orphan_ctx()
            cs_log.cs_log(
                logging.WARNING,
                "[content_sync] no AYON VizDev task for concept %s; "
                "skip preview %s (push concept first)",
                (entity_id or "")[:8],
                preview_file_id,
            )
            return

        ayon_task_id = ayon_task["id"] if ayon_task else None

        revision = preview.get("revision", 1)
        comment_id = preview.get("comment_id", "")
        kitsu_comment_id_str = str(comment_id).strip() if comment_id else ""

        email_cache: dict[str, str] = {}
        kitsu_person = _kitsu_person_dict_for_preview_uploader(
            preview, kitsu_comment_id_str,
        )
        raw_pe = kitsu_person.get("email") if isinstance(kitsu_person.get("email"), str) else None
        pe = raw_pe.strip() if raw_pe and raw_pe.strip() else None
        raw_kfn = kitsu_person.get("full_name") if isinstance(kitsu_person.get("full_name"), str) else None
        kitsu_fn = raw_kfn.strip() if raw_kfn and raw_kfn.strip() else None
        fn_index = _full_name_login_index_if_enabled(processor)
        ayon_login = _resolve_ayon_login_for_comment_sync(
            pe,
            project_name,
            email_cache,
            kitsu_full_name=kitsu_fn,
            full_name_index=fn_index,
        )

        product_name: str | None = None
        product_extra: dict[str, Any] | None = None
        if link_from_surrogate or is_pool_surrogate:
            src_concept = (source_kitsu_concept_id or "").strip() or None
            if not src_concept:
                src_concept = _preview_source_concept_id_from_payload(preview)
            product_name = _per_linked_concept_review_product_name(
                task_type,
                source_kitsu_concept_id=src_concept,
                preview_file_id=str(preview_file_id),
            )
            product_extra = (
                {"kitsuSourceConceptId": src_concept} if src_concept else None
            )

        product_id = _find_or_create_review_product(
            project_name,
            ayon_folder["id"],
            task_type,
            resolved_task_id,
            product_name=product_name,
            product_data_extra=product_extra,
        )
        existing_vid = _find_review_version_id_for_revision(
            project_name, product_id, revision,
        )
        if existing_vid:
            version_id = existing_vid
        else:
            created_wrap: dict[str, str] = {}

            def _create_review_version_wrapped() -> None:
                created_wrap["id"] = _create_review_version_entity(
                    project_name,
                    product_id,
                    revision,
                    resolved_task_id,
                    kitsu_comment_id_str,
                    ayon_task_id,
                    ayon_login,
                )

            _run_ayon_as_kitsu_person_when_service(
                processor,
                project_name,
                kitsu_person if isinstance(kitsu_person, dict) else {},
                email_cache,
                _create_review_version_wrapped,
                acl_log_label="Review version",
            )
            version_id = created_wrap["id"]

        ver_snapshot = ayon_api.get_version_by_id(project_name, version_id)
        _ensure_review_version_author_with_impersonation(
            processor,
            project_name,
            version_id,
            ver_snapshot,
            kitsu_person,
            email_cache,
            ayon_login,
        )
        ver_snapshot = ayon_api.get_version_by_id(project_name, version_id)
        if _preview_file_id_in_version_data(preview_file_id, ver_snapshot):
            _ensure_preview_orphan_ctx()
            cs_log.cs_log(
                logging.INFO,
                "[content_sync] skip preview upload: version already lists this Kitsu "
                "preview file id (version_id=%s preview_file_id=%s)",
                version_id,
                preview_file_id,
            )
            return

        ext = preview.get("extension", "png")
        original_name = preview.get("original_name", f"{preview_file_id}.{ext}")
        url = _preview_download_url(preview)

        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
            tmp_path = tmp.name

        main_ok = False
        pdf_project_file_id: str | None = None
        preview_kind_for_merge: str | None = None
        try:
            gazu.client.download(url, tmp_path)

            def _preview_upload_and_merge() -> None:
                nonlocal main_ok, pdf_project_file_id, preview_kind_for_merge
                try:
                    try:
                        sz = os.path.getsize(tmp_path)
                    except OSError:
                        sz = -1
                    head_hex = ""
                    try:
                        with open(tmp_path, "rb") as rh:
                            head_hex = rh.read(16).hex()
                    except OSError:
                        pass
                    log.debug(
                        "[content_sync] reviewable_prepare preview_file_id=%s revision=%s position=%s "
                        "comment_id=%s original_name=%r kitsu_extension=%r download_path=%r "
                        "tmp_suffix=%r size=%s head16_hex=%s",
                        preview_file_id,
                        preview.get("revision"),
                        preview.get("position"),
                        preview.get("comment_id"),
                        original_name,
                        ext,
                        url,
                        Path(tmp_path).suffix,
                        sz,
                        head_hex or None,
                    )
                    upload_name, content_type = _infer_reviewable_basename_and_mime(
                        tmp_path,
                        preview,
                        original_name,
                        preview_file_id,
                        str(ext),
                    )
                    _ensure_preview_orphan_ctx()
                    if upload_name and content_type:
                        log.debug(
                            "[content_sync] reviewable_upload preview_file_id=%s filename=%r "
                            "content_type=%s",
                            preview_file_id,
                            upload_name,
                            content_type,
                        )
                        try:
                            ayon_api.upload_reviewable(
                                project_name,
                                version_id,
                                tmp_path,
                                label=None,
                                filename=upload_name,
                                content_type=content_type,
                            )
                            main_ok = True
                            cs_log.cs_log(
                                logging.INFO,
                                "Reviewable uploaded: %s -> version %s",
                                original_name,
                                version_id,
                            )
                        except (HTTPRequestError, requests.exceptions.HTTPError) as up_exc:
                            if (
                                _http_error_status(up_exc) == 400
                                and _http_error_detail_contains(
                                    up_exc,
                                    "Failed to extract media info",
                                )
                            ):
                                cs_log.cs_log(
                                    logging.WARNING,
                                    "[content_sync] reviewable_extract_media_info_failed "
                                    "preview_file_id=%s filename=%r; storing_as_project_file",
                                    preview_file_id,
                                    upload_name,
                                )
                                try:
                                    resp = ayon_api.upload_project_file(
                                        project_name,
                                        tmp_path,
                                        filename=upload_name,
                                    )
                                    resp_data = resp.json() if hasattr(resp, "json") else {}
                                    fid = str(resp_data.get("id", "") or "")
                                    if fid:
                                        pdf_project_file_id = fid
                                        preview_kind_for_merge = "extract_failed_sidecar"
                                        main_ok = True
                                        cs_log.cs_log(
                                            logging.INFO,
                                            "[content_sync] stored Kitsu preview as project file "
                                            "(reviewable media extract failed; preview_file_id=%s "
                                            "project_file_id=%s filename=%r)",
                                            preview_file_id,
                                            fid,
                                            upload_name,
                                        )
                                    else:
                                        cs_log.cs_log(
                                            logging.WARNING,
                                            "[content_sync] preview_extract_fallback_no_file_id "
                                            "preview_file_id=%s",
                                            preview_file_id,
                                        )
                                except Exception as pf_exc:
                                    cs_log.cs_log(
                                        logging.ERROR,
                                        "[content_sync] preview_extract_fallback_upload_failed "
                                        "preview_file_id=%s: %s%s",
                                        preview_file_id,
                                        pf_exc,
                                        _http_error_body_snippet(pf_exc),
                                    )
                            else:
                                raise
                    elif _is_pdf_preview(tmp_path, str(ext)):
                        pdf_filename = _reviewable_upload_basename(
                            original_name, preview_file_id, "pdf",
                        )
                        log.debug(
                            "[content_sync] preview_pdf_project_file preview_file_id=%s filename=%r",
                            preview_file_id,
                            pdf_filename,
                        )
                        resp = ayon_api.upload_project_file(
                            project_name,
                            tmp_path,
                            filename=pdf_filename,
                        )
                        resp_data = resp.json() if hasattr(resp, "json") else {}
                        fid = str(resp_data.get("id", "") or "")
                        if fid:
                            pdf_project_file_id = fid
                            preview_kind_for_merge = "pdf"
                            main_ok = True
                            cs_log.cs_log(
                                logging.INFO,
                                "[content_sync] preview_pdf_uploaded preview_file_id=%s "
                                "project_file_id=%s version_id=%s filename=%r",
                                preview_file_id,
                                fid,
                                version_id,
                                pdf_filename,
                            )
                        else:
                            cs_log.cs_log(
                                logging.WARNING,
                                "[content_sync] preview_pdf_upload_no_file_id preview_file_id=%s",
                                preview_file_id,
                            )
                    else:
                        cs_log.cs_log(
                            logging.WARNING,
                            "[content_sync] skip preview upload: unsupported or unknown media "
                            "type for direct reviewable (preview_file_id=%s original_name=%r "
                            "extension=%r size=%s head16_hex=%s)",
                            preview_file_id,
                            original_name,
                            ext,
                            sz,
                            head_hex or None,
                        )
                except Exception as exc:
                    cs_log.cs_log(
                        logging.ERROR,
                        "Reviewable upload failed for %s: %s%s",
                        preview_file_id,
                        exc,
                        _http_error_body_snippet(exc),
                    )
                    return
                if not main_ok:
                    return
                ann_payload = (
                    preview.get("annotations")
                    if preview.get("annotations") is not None
                    else []
                )
                _merge_kitsu_preview_metadata_on_version(
                    project_name,
                    version_id,
                    str(preview_file_id),
                    ann_payload,
                    preview,
                    project_file_id=pdf_project_file_id,
                    preview_kind=preview_kind_for_merge,
                )
                _merge_kitsu_preview_file_ids_on_version(
                    project_name, version_id, [preview_file_id],
                )

            _run_ayon_as_kitsu_person_when_service(
                processor,
                project_name,
                kitsu_person if isinstance(kitsu_person, dict) else {},
                email_cache,
                _preview_upload_and_merge,
                acl_log_label="Review upload",
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    finally:
        if _orphan_line_tok is not None:
            cs_log.line_prefix_reset(_orphan_line_tok)


# ---------------------------------------------------------------------------
# Comment sync
# ---------------------------------------------------------------------------


def _normalize_kitsu_preview_entries(raw_previews: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not raw_previews:
        return rows
    for p in raw_previews:
        if isinstance(p, dict):
            rows.append(p)
        elif isinstance(p, str) and p.strip():
            sid = p.strip()
            rows.append({"id": sid, "position": 0, "original_name": sid})
    return rows


def _kitsu_preview_rows_informative_for_comment_appendix(
    rows: list[dict[str, Any]],
) -> bool:
    """True when Kitsu preview rows carry a real revision label and a non-id filename."""
    if not rows:
        return False
    sorted_prev = sorted(rows, key=lambda row: row.get("position", 0))
    lead = sorted_prev[0]
    rev = lead.get("revision")
    if rev is None or rev == "" or rev == "?":
        return False
    pid = str(lead.get("id") or "").strip()
    oname = lead.get("original_name")
    if not isinstance(oname, str) or not oname.strip():
        return False
    if oname.strip() == pid:
        return False
    return True


def _should_append_kitsu_preview_manifest_to_comment_body(
    processor: "KitsuProcessor",
    preview_rows: list[dict[str, Any]],
) -> bool:
    """Append 'Revision / review files' block only for rich preview metadata, unless legacy setting forces it."""
    if not preview_rows:
        return False
    block = _content_sync_settings_block(processor)
    if block.get("append_kitsu_preview_manifest") is True:
        return True
    return _kitsu_preview_rows_informative_for_comment_appendix(preview_rows)


def _comment_has_nonempty_checklist(comment: dict) -> bool:
    cl = comment.get("checklist") or []
    if not isinstance(cl, list):
        return False
    for item in cl:
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            return True
        if isinstance(item, str) and item.strip():
            return True
    return False


def _content_sync_settings_block(processor: "KitsuProcessor") -> dict[str, Any]:
    sync = (getattr(processor, "settings", None) or {}).get("sync_settings") or {}
    block = sync.get("content_sync") if isinstance(sync, dict) else None
    return block if isinstance(block, dict) else {}


def attach_comment_preview_json_sidecars_enabled(processor: "KitsuProcessor") -> bool:
    """Legacy: attach ``*_annotations.json`` / ``*_preview_file.json`` to comment activities (default off)."""
    return bool(_content_sync_settings_block(processor).get("attach_comment_preview_json_sidecars", False))


def resolve_author_login_by_full_name_enabled(processor: "KitsuProcessor") -> bool:
    """When True, map Kitsu ``full_name`` to AYON login when email lookup fails (opt-in; collision-prone)."""
    return _content_sync_settings_block(processor).get("resolve_author_login_by_full_name") is True


def impersonate_comment_authors_enabled(processor: "KitsuProcessor") -> bool:
    """When True (default), run matching AYON writes as the Kitsu author via ``as_username`` when possible.

    Covers: comment ``create_activity``, comment attachment and preview-sidecar
    ``upload_project_file``, review version author/create, Kitsu preview
    ``upload_reviewable`` / preview ``upload_project_file`` plus preview metadata
    merges on the version, and checklist subtask upserts (see
    ``checklist_subtask_sync``). ``get_activities`` / ``delete_activity`` stay on
    the service API identity.
    """
    return _content_sync_settings_block(processor).get("impersonate_comment_authors", True) is not False


def _acl_forbidden_comment_sync(exc: BaseException) -> bool:
    s = str(exc).lower()
    return (
        "403" in s
        or "forbidden" in s
        or "permission" in s
        or "not allowed" in s
        or "access denied" in s
    )


def _normalize_kitsu_full_name_key(full_name: str) -> str:
    return " ".join(full_name.split()).strip().lower()


def _build_ayon_users_by_normalized_full_name() -> dict[str, str]:
    """Map normalized ``attrib.fullName`` / ``attrib.full_name`` to AYON login ``name``."""
    out: dict[str, str] = {}
    gu = getattr(ayon_api, "get_users", None)
    if not callable(gu):
        return out
    try:
        users = gu()
    except Exception as exc:  # noqa: BLE001
        log.warning("get_users for full-name login index failed: %s", exc)
        return out
    for u in users or []:
        if not isinstance(u, dict) or not u.get("name"):
            continue
        att = u.get("attrib")
        if not isinstance(att, dict):
            att = {}
        fn = att.get("fullName") or att.get("full_name")
        if not isinstance(fn, str) or not fn.strip():
            continue
        key = _normalize_kitsu_full_name_key(fn)
        if key and key not in out:
            out[key] = str(u["name"])
    return out


def _full_name_login_index_if_enabled(
    processor: "KitsuProcessor",
) -> dict[str, str] | None:
    if not resolve_author_login_by_full_name_enabled(processor):
        return None
    return _build_ayon_users_by_normalized_full_name()


def _resolve_ayon_login_for_comment_sync(
    email_or_login: str | None,
    project_name: str,
    cache: dict[str, str],
    *,
    kitsu_full_name: str | None = None,
    full_name_index: dict[str, str] | None = None,
) -> str | None:
    if email_or_login and str(email_or_login).strip():
        c = str(email_or_login).strip()
        if "@" not in c:
            return c
        key = c.lower()
        if key in cache:
            return cache[key]
        gu = getattr(ayon_api, "get_users", None)
        if not callable(gu):
            return None
        first: dict[str, Any] | None = None
        it = None
        try:
            try:
                it = gu(project_name=project_name, emails=[key], fields={"name"})  # type: ignore[call-arg]
            except TypeError:
                it = gu(project_name, emails=[key], fields={"name"})  # type: ignore[call-arg]
        except (TypeError, ValueError) as exc:
            log.debug("Impersonation email lookup failed: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("Impersonation get_users failed: %s", exc)
            return None
        for u in it or []:
            if isinstance(u, dict) and u.get("name"):
                first = u
                break
        if first:
            name = str(first["name"])
            cache[key] = name
            return name
    if full_name_index and kitsu_full_name and kitsu_full_name.strip():
        nk = _normalize_kitsu_full_name_key(kitsu_full_name)
        if nk:
            hit = full_name_index.get(nk)
            if hit:
                return hit
    return None


def _run_ayon_as_kitsu_person_when_service(
    processor: "KitsuProcessor",
    project_name: str,
    person: dict[str, Any],
    email_cache: dict[str, str],
    fn: Callable[[], None],
    *,
    acl_log_label: str = "Comment sync",
) -> None:
    """Run ``fn`` under ``as_username`` when service key + Kitsu person maps to an AYON login."""
    if not impersonate_comment_authors_enabled(processor):
        fn()
        return
    try:
        con = ayon_api.get_server_api_connection()
    except Exception as exc:
        log.debug("No server connection for impersonation: %s", exc)
        fn()
        return
    is_svc_fn = getattr(con, "is_service_user", None)
    if not callable(is_svc_fn) or not is_svc_fn():
        fn()
        return
    raw_email = person.get("email") if isinstance(person, dict) else None
    email = raw_email.strip() if isinstance(raw_email, str) and raw_email.strip() else None
    raw_fn = person.get("full_name") if isinstance(person, dict) else None
    kitsu_fn = raw_fn.strip() if isinstance(raw_fn, str) and raw_fn.strip() else None
    fn_index = _full_name_login_index_if_enabled(processor)
    login = _resolve_ayon_login_for_comment_sync(
        email,
        project_name,
        email_cache,
        kitsu_full_name=kitsu_fn,
        full_name_index=fn_index,
    )
    if not login:
        fn()
        return
    as_u = getattr(con, "as_username", None)
    if not callable(as_u):
        fn()
        return
    try:
        with as_u(login):
            fn()
    except Exception as exc:  # noqa: BLE001
        if _acl_forbidden_comment_sync(exc):
            log.warning(
                "%s: impersonation ACL failure as %r, retrying as service user: %s",
                acl_log_label,
                login,
                exc,
            )
            fn()
        else:
            raise


def _run_comment_sync_mutations(
    processor: "KitsuProcessor",
    project_name: str,
    person: dict[str, Any],
    email_cache: dict[str, str],
    fn: Callable[[], None],
) -> None:
    """Run ``fn`` as the Kitsu author when using a service API key.

    ``sync_comment_to_ayon`` normally passes a callable that performs comment
    ``upload_project_file`` and ``create_activity`` together. Deletes and
    ``get_activities`` stay outside this wrapper (service identity).
    """
    _run_ayon_as_kitsu_person_when_service(
        processor, project_name, person, email_cache, fn, acl_log_label="Comment sync",
    )


def _web_ui_base_url_for_review_version_link(processor: "KitsuProcessor") -> str:
    block = _content_sync_settings_block(processor)
    raw = block.get("web_ui_base_url")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().rstrip("/")
    try:
        fn = getattr(ayon_api, "get_base_url", None)
        base = (fn() or "").strip().rstrip("/") if callable(fn) else ""
    except Exception:
        base = ""
    if base.endswith("/api"):
        base = base[: -len("/api")].rstrip("/")
    return base


def _folder_path_for_ayon_products_uri(folder: dict) -> str:
    if not isinstance(folder, dict):
        return ""
    p = folder.get("path")
    if isinstance(p, str) and p.strip():
        return p.strip().lstrip("/")
    return ""


def _kitsu_task_type_name_for_review_product(kitsu_task: dict) -> str:
    tt = kitsu_task.get("task_type") if isinstance(kitsu_task, dict) else None
    if isinstance(tt, dict):
        n = (tt.get("name") or "").strip()
        if n:
            return n
    raw = kitsu_task.get("task_type_name") if isinstance(kitsu_task, dict) else None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return "unknown"


def _revision_from_comment_for_review_link(comment: dict) -> int | None:
    rows = _normalize_kitsu_preview_entries(comment.get("previews") or [])
    if rows:
        sorted_prev = sorted(rows, key=lambda row: int(row.get("position") or 0))
        for row in sorted_prev:
            rev_raw = row.get("revision")
            if rev_raw is None or rev_raw == "" or rev_raw == "?":
                continue
            try:
                r = int(rev_raw)
            except (TypeError, ValueError):
                continue
            if r > 0:
                return r
    text = comment.get("text") or ""
    if isinstance(text, str):
        parsed = browser_urls.parse_kitsu_publish_comment_table_version(text)
        if parsed is not None:
            return parsed
    return None


def _comment_body_has_version_markdown_link(body: str, version_num: int) -> bool:
    return bool(re.search(rf"\[Version\s*{version_num}\]\(", body or ""))


def _maybe_append_review_version_markdown_link(
    processor: "KitsuProcessor",
    project_name: str,
    kitsu_task_id: str,
    comment: dict,
    body: str,
) -> str:
    block = _content_sync_settings_block(processor)
    if block.get("review_version_link_enabled") is False:
        return body
    if not isinstance(body, str):
        return body
    rev = _revision_from_comment_for_review_link(comment)
    if not rev or rev < 1:
        return body

    web_base = _web_ui_base_url_for_review_version_link(processor)
    if not web_base:
        log.debug(
            "[content_sync] skip review version link: empty web UI base "
            "(kitsu_comment_id=%s)",
            str(comment.get("id", ""))[:8],
        )
        return body

    if _comment_body_has_version_markdown_link(body, rev):
        return body

    try:
        kitsu_task = gazu.task.get_task(kitsu_task_id)
    except Exception as exc:
        log.debug(
            "[content_sync] get_task for review version link kitsu=%s: %s",
            (kitsu_task_id or "")[:8],
            exc,
        )
        return body
    if not isinstance(kitsu_task, dict):
        return body

    ayon_task = _ayon_task_by_kitsu_id(project_name, kitsu_task_id)
    if not ayon_task:
        return body
    folder_id = _task_folder_id(ayon_task)
    if not folder_id:
        return body

    folder: dict | None = None
    gfb = getattr(ayon_api, "get_folder_by_id", None)
    if callable(gfb):
        try:
            folder = gfb(project_name, folder_id)
        except Exception:
            folder = None
    if not folder or not isinstance(folder, dict):
        return body

    folder_path = _folder_path_for_ayon_products_uri(folder)
    if not folder_path:
        log.debug(
            "[content_sync] skip review version link: folder has no path "
            "(folder_id=%s)",
            str(folder_id)[:8],
        )
        return body

    tt_name = _kitsu_task_type_name_for_review_product(kitsu_task)
    product_name = f"{tt_name.lower()}KitsuReview"

    product_id: str | None = None
    try:
        for prod in ayon_api.get_products(project_name, folder_ids=[str(folder_id)]):
            if not isinstance(prod, dict):
                continue
            pt = prod.get("productType") or prod.get("product_type")
            if str(pt or "").lower() != "review":
                continue
            if prod.get("name") == product_name:
                pid = prod.get("id")
                if pid:
                    product_id = str(pid)
                break
    except Exception as exc:
        log.debug("[content_sync] get_products for review version link: %s", exc)
        return body

    if not product_id:
        log.debug(
            "[content_sync] skip review version link: no review product %r "
            "(kitsu_task_id=%s revision=%s)",
            product_name,
            (kitsu_task_id or "")[:8],
            rev,
        )
        return body

    version_id = _find_review_version_id_for_revision(
        project_name, product_id, rev,
    )
    if not version_id:
        log.debug(
            "[content_sync] skip review version link: no version entity "
            "revision=%s product=%s",
            rev,
            product_name,
        )
        return body

    entity_uri = browser_urls.ayon_entity_uri_product_version(
        project_name,
        folder_path,
        product_name=product_name,
        version=rev,
    )
    href = browser_urls.ayon_browser_url_products_with_uri(
        web_base, project_name, entity_uri,
    )
    if not href:
        return body
    if href in body:
        return body

    suffix_line = f"[Version {rev}]({href})"
    proposed = body.rstrip() + "\n\n" + suffix_line + "\n"
    if len(proposed) > _MAX_AYON_ACTIVITY_BODY_CHARS * 4:
        log.debug(
            "[content_sync] skip review version link: body too large after append",
        )
        return body
    return proposed


def _build_comment_body(
    comment: dict,
    persons: dict[str, dict],
    statuses: dict[str, str],
    processor: "KitsuProcessor",
    *,
    suppress_attribution_header: bool = False,
) -> str:
    person = persons.get(comment.get("person_id", ""), {})
    if not isinstance(person, dict):
        person = {}
    author = person.get("full_name", "Unknown")
    status_name = statuses.get(comment.get("task_status_id", ""), "")

    parts: list[str] = []
    if not suppress_attribution_header:
        header = f"**[{author}]**"
        if status_name:
            header += f" -- _{status_name}_"
        parts.append(header)
        parts.append("")

    text = comment.get("text") or ""
    if text:
        parts.append(text)

    checklist = comment.get("checklist") or []
    if checklist:
        parts.append("")
        for item in checklist:
            if isinstance(item, dict):
                chk = "x" if item.get("checked") else " "
                parts.append(f"- [{chk}] {item.get('text', '')}")
            elif isinstance(item, str) and item.strip():
                parts.append(f"- [ ] {item.strip()}")

    preview_rows = _normalize_kitsu_preview_entries(comment.get("previews") or [])
    if _should_append_kitsu_preview_manifest_to_comment_body(processor, preview_rows):
        sorted_prev = sorted(
            preview_rows, key=lambda row: row.get("position", 0)
        )
        rev = sorted_prev[0].get("revision", "?")
        parts.append("")
        parts.append("---")
        parts.append(f"**Revision {rev}** ({len(sorted_prev)} review files)")
        for row in sorted_prev:
            rid = row.get("id", "?")
            parts.append(f"- `{row.get('original_name', rid)}`")

    return "\n".join(parts) + "\n"


def sync_comment_to_ayon(
    processor: "KitsuProcessor",
    comment_id: str,
    task_id: str,
    project_id: str,
    *,
    persons_by_id: dict[str, dict] | None = None,
    statuses_by_id: dict[str, str] | None = None,
    log_progress: bool = False,
):
    """Sync a single Kitsu comment to AYON as one or more comment activities (multipart).

    When ``persons_by_id`` / ``statuses_by_id`` are provided (e.g. fullsync task loop),
    Kitsu roster calls are skipped for that invocation.

    When ``log_progress`` is True (repair driver only), emit INFO lines around slow
    steps (Kitsu attachment downloads, AYON uploads, activity delete/create).

    Kitsu ``attachment_files`` become AYON activity file attachments (uploaded
    under ``as_username`` when impersonation applies, same as ``create_activity``).
    Kitsu ``previews`` do not imply AYON reviewables here (reviewables use preview sync).
    A markdown preview manifest is omitted unless metadata is informative or
    ``append_kitsu_preview_manifest`` is true; comments that would only carry
    noise have their matching AYON activities removed and none created.
    """
    project_name = processor.get_paired_ayon_project(project_id)
    if not project_name:
        return

    processor_utils.set_kitsu_host(processor.kitsu_server_url)

    try:
        comment = gazu.task.get_comment(comment_id)
    except Exception as exc:
        cs_log.cs_log(logging.ERROR, "Failed to get comment %s: %s", comment_id, exc)
        return

    if not isinstance(comment, dict):
        cs_log.cs_log(
            logging.WARNING,
            "get_comment returned %s for %s; expected dict",
            type(comment).__name__,
            comment_id[:8],
        )
        return

    ayon_task = _ayon_task_by_kitsu_id(project_name, task_id)
    if not ayon_task:
        log.debug("No AYON task for Kitsu task %s", task_id)
        return
    ayon_task_id = ayon_task["id"]

    if persons_by_id is None:
        persons_raw = gazu.person.all_persons()
        persons = {p["id"]: p for p in persons_raw}
    else:
        persons = persons_by_id
    if statuses_by_id is None:
        statuses_raw = gazu.task.all_task_statuses()
        statuses = {s["id"]: s.get("name", "") for s in statuses_raw}
    else:
        statuses = statuses_by_id

    suppress_header = impersonate_comment_authors_enabled(processor)
    person = persons.get(comment.get("person_id", ""), {})
    if not isinstance(person, dict):
        person = {}
    author_name = person.get("full_name", "Unknown")
    status_name = statuses.get(comment.get("task_status_id", ""), "")

    email_cache: dict[str, str] = {}

    body_local = _build_comment_body(
        comment,
        persons,
        statuses,
        processor,
        suppress_attribution_header=suppress_header,
    )

    preview_rows_norm = _normalize_kitsu_preview_entries(comment.get("previews") or [])
    would_append_manifest = _should_append_kitsu_preview_manifest_to_comment_body(
        processor, preview_rows_norm,
    )
    attachment_dicts = [
        a
        for a in (comment.get("attachment_files") or [])
        if isinstance(a, dict)
    ]
    will_try_sidecars = (
        attach_comment_preview_json_sidecars_enabled(processor)
        and bool(preview_rows_norm)
    )
    noise_only_skip_activity = (
        not (comment.get("text") or "").strip()
        and not _comment_has_nonempty_checklist(comment)
        and not attachment_dicts
        and not will_try_sidecars
        and not would_append_manifest
    )
    if noise_only_skip_activity:
        existing_noise = list(
            ayon_api.get_activities(
                project_name,
                entity_ids=[ayon_task_id],
                activity_types=["comment"],
            ),
        )
        _delete_all_activities_for_kitsu_comment(
            project_name,
            ayon_task_id,
            comment_id,
            activities=existing_noise,
        )
        maybe_sync_checklist_subtasks_from_kitsu_comment(
            processor, task_id, comment_id, project_id,
        )
        log.debug(
            "[content_sync] skip comment activity: no text/checklist/attachments "
            "and no informative Kitsu preview manifest (kitsu_comment_id=%s)",
            comment_id[:8],
        )
        return

    if log_progress:
        log.info(
            "[repair_comments] comment %s task=%s attachments=%s preview_rows=%s "
            "sidecar_json=%s",
            comment_id[:8],
            (task_id or "")[:8],
            len(attachment_dicts),
            len(preview_rows_norm),
            will_try_sidecars,
        )

    if log_progress:
        log.info(
            "[repair_comments] comment %s resolving optional review-version link …",
            comment_id[:8],
        )
    body_local = _maybe_append_review_version_markdown_link(
        processor, project_name, task_id, comment, body_local,
    )

    attachment_jobs: list[tuple[str, str, dict[str, Any]]] = []
    for att in attachment_dicts:
        att_name = att.get("name", att["id"])
        with tempfile.NamedTemporaryFile(
            suffix=f".{att.get('extension', 'bin')}", delete=False,
        ) as tmp:
            tmp_path = tmp.name
        try:
            if log_progress:
                log.info(
                    "[repair_comments] comment %s downloading Kitsu attachment id=%s "
                    "name=%r",
                    comment_id[:8],
                    att.get("id"),
                    att_name,
                )
            gazu.files.download_attachment_file(att, tmp_path)
            attachment_jobs.append((tmp_path, str(att_name), att))
        except Exception as exc:
            cs_log.cs_log(
                logging.WARNING,
                "Attachment download failed %s: %s",
                att.get("id"),
                exc,
            )
            Path(tmp_path).unlink(missing_ok=True)

    sidecar_jobs: list[tuple[str, str]] = []
    if will_try_sidecars:
        if log_progress:
            log.info(
                "[repair_comments] comment %s staging %s preview JSON sidecar(s) …",
                comment_id[:8],
                len(preview_rows_norm),
            )
        for pv in sorted(
            preview_rows_norm,
            key=lambda x: int(x.get("position") or 0),
        ):
            pid = pv["id"]
            pos = int(pv.get("position") or 0)
            base = f"{pos:02d}_{pid}"
            try:
                pfile = gazu.files.get_preview_file(pid)
            except Exception as exc:
                cs_log.cs_log(
                    logging.WARNING,
                    "get_preview_file %s for comment sidecar: %s",
                    pid,
                    exc,
                )
                continue
            if not pfile:
                continue
            ann_payload = {
                "preview_file_id": pid,
                "annotations": pfile.get("annotations")
                if pfile.get("annotations") is not None
                else [],
            }
            for fname, payload in (
                (f"{base}_annotations.json", ann_payload),
                (f"{base}_preview_file.json", pfile),
            ):
                pj_tmp: str | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w", suffix=".json", delete=False, encoding="utf-8",
                    ) as pj_f:
                        json.dump(payload, pj_f, default=str)
                        pj_tmp = pj_f.name
                    sidecar_jobs.append((pj_tmp, fname))
                except Exception as exc:
                    cs_log.cs_log(
                        logging.WARNING,
                        "Comment preview sidecar staging failed %s: %s",
                        fname,
                        exc,
                    )
                    if pj_tmp:
                        Path(pj_tmp).unlink(missing_ok=True)

    upload_state: dict[str, Any] = {
        "file_ids": [],
        "body": body_local,
        "uploaded_sidecars": False,
    }

    def _upload_comment_files_to_ayon() -> None:
        fids: list[str] = list(upload_state["file_ids"])
        for tmp_path, att_name, att in attachment_jobs:
            try:
                resp = ayon_api.upload_project_file(
                    project_name, tmp_path, filename=att_name,
                )
                resp_data = resp.json() if hasattr(resp, "json") else {}
                fid = resp_data.get("id", "")
                if fid:
                    fids.append(str(fid))
            except Exception as exc:
                cs_log.cs_log(
                    logging.WARNING,
                    "Attachment upload failed %s: %s",
                    att.get("id"),
                    exc,
                )
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        for pj_tmp, fname in sidecar_jobs:
            try:
                resp = ayon_api.upload_project_file(
                    project_name, pj_tmp, filename=fname,
                )
                resp_data = resp.json() if hasattr(resp, "json") else {}
                fid = resp_data.get("id", "")
                if fid:
                    fids.append(str(fid))
                    upload_state["uploaded_sidecars"] = True
            except Exception as exc:
                cs_log.cs_log(
                    logging.WARNING,
                    "Comment preview sidecar upload failed %s: %s",
                    fname,
                    exc,
                )
            finally:
                Path(pj_tmp).unlink(missing_ok=True)
        upload_state["file_ids"] = fids
        if upload_state["uploaded_sidecars"]:
            upload_state["body"] = (
                str(upload_state["body"]).rstrip()
                + "\n\n_Kitsu annotation and preview metadata JSON files are attached._\n"
            )

    if log_progress and (attachment_jobs or sidecar_jobs):
        log.info(
            "[repair_comments] comment %s uploading to AYON: %s attachment(s) "
            "+ %s sidecar(s) …",
            comment_id[:8],
            len(attachment_jobs),
            len(sidecar_jobs),
        )
    _run_comment_sync_mutations(
        processor, project_name, person, email_cache, _upload_comment_files_to_ayon,
    )

    file_ids: list[str] = list(upload_state["file_ids"])
    full_body = str(upload_state["body"])
    body_sha256 = _sha256_utf8(full_body)
    part_bodies = _split_comment_body_for_ayon_activities(full_body)
    part_count = len(part_bodies)

    if log_progress:
        log.info(
            "[repair_comments] comment %s checking AYON activities (parts=%s) …",
            comment_id[:8],
            part_count,
        )
    existing = list(ayon_api.get_activities(
        project_name, entity_ids=[ayon_task_id], activity_types=["comment"],
    ))
    matches = _activities_for_kitsu_comment_id(existing, comment_id)
    if _existing_comment_sync_uptodate(matches, part_count, body_sha256, full_body):
        log.debug(
            "[content_sync] skip comment sync: AYON task activities already match "
            "this Kitsu comment body and parts (kitsu_comment_id=%s part_count=%s)",
            comment_id,
            part_count,
        )
        maybe_sync_checklist_subtasks_from_kitsu_comment(
            processor, task_id, comment_id, project_id,
        )
        return

    _delete_all_activities_for_kitsu_comment(
        project_name,
        ayon_task_id,
        comment_id,
        activities=existing,
    )

    if log_progress:
        log.info(
            "[repair_comments] comment %s creating %s AYON activity part(s) …",
            comment_id[:8],
            part_count,
        )
    base_data: dict[str, Any] = {
        "kitsuCommentId": comment_id,
        "kitsuAuthor": author_name,
        "kitsuCommentBodySha256": body_sha256,
    }
    if status_name:
        base_data["kitsuStatusChange"] = status_name
    if comment.get("pinned"):
        base_data["kitsuPinned"] = True
    pe = person.get("email")
    if isinstance(pe, str) and pe.strip():
        base_data["kitsuAuthorEmail"] = pe.strip()

    def _create_comment_activities() -> None:
        # Idempotent for ACL retry: impersonation may create part 1 then fail; service
        # retry must not duplicate parts (outer delete already ran once; this clears partial).
        _delete_all_activities_for_kitsu_comment(
            project_name, ayon_task_id, comment_id,
        )
        try:
            for part_index, part_body in enumerate(part_bodies, start=1):
                data = {
                    **base_data,
                    "kitsuCommentPart": part_index,
                    "kitsuCommentPartCount": part_count,
                }
                fids = file_ids if part_index == 1 else None
                aid = ayon_api.create_activity(
                    project_name,
                    entity_id=ayon_task_id,
                    entity_type="task",
                    activity_type="comment",
                    body=part_body,
                    file_ids=fids if fids else None,
                    timestamp=comment.get("created_at"),
                    data=data,
                )
                prev = _comment_body_preview(comment)
                cs_log.cs_log(
                    logging.INFO,
                    "Activity created %s for comment %s part %s/%s%s",
                    aid,
                    comment_id[:8],
                    part_index,
                    part_count,
                    f" preview={prev!r}" if prev else "",
                )
        finally:
            maybe_sync_checklist_subtasks_from_kitsu_comment(
                processor, task_id, comment_id, project_id,
            )

    _run_comment_sync_mutations(
        processor, project_name, person, email_cache, _create_comment_activities,
    )


def delete_comment_from_ayon(
    processor: "KitsuProcessor",
    comment_id: str,
    task_id: str,
    project_id: str,
):
    """Delete all AYON comment activities that correspond to a deleted Kitsu comment."""
    project_name = processor.get_paired_ayon_project(project_id)
    if not project_name:
        return

    ayon_task = _ayon_task_by_kitsu_id(project_name, task_id)
    if not ayon_task:
        return

    delete_checklist_subtasks_for_comment(
        processor, project_name, comment_id, task_id,
    )

    _delete_all_activities_for_kitsu_comment(
        project_name, ayon_task["id"], comment_id,
    )


def update_comment_on_ayon(
    processor: "KitsuProcessor",
    comment_id: str,
    task_id: str,
    project_id: str,
):
    """Replace AYON activities for an edited Kitsu comment (multipart-aware)."""
    sync_comment_to_ayon(
        processor,
        comment_id,
        task_id,
        project_id,
    )


# ---------------------------------------------------------------------------
# Full-project content sync (called from fullsync.py)
# ---------------------------------------------------------------------------

def _iter_review_products_for_project(project_name: str) -> Iterator[dict[str, Any]]:
    """Yield review-type products for ``project_name`` (handles ayon_api signature variants)."""
    gp = ayon_api.get_products
    attempts = (
        lambda: gp(project_name, product_types=["review"]),
        lambda: gp(project_name, product_type="review"),
    )
    for attempt in attempts:
        try:
            for p in attempt():
                if isinstance(p, dict):
                    yield p
            return
        except TypeError:
            continue
    try:
        folders = ayon_api.get_folders(project_name)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "[repair_version_authors] get_folders failed project=%s: %s",
            project_name,
            exc,
        )
        return
    for folder in folders or []:
        if not isinstance(folder, dict):
            continue
        fid = folder.get("id")
        if not fid:
            continue
        try:
            prods = gp(project_name, folder_ids=[str(fid)], product_types=["review"])
        except TypeError:
            try:
                prods = gp(project_name, folder_ids=[str(fid)])
            except Exception:
                continue
        for p in prods or []:
            if not isinstance(p, dict):
                continue
            pt = p.get("productType") or p.get("product_type")
            if str(pt).lower() == "review":
                yield p


def _pick_kitsu_preview_for_version_repair(
    previews: list[dict[str, Any]],
    revision: int,
    kitsu_comment_id: str | None,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for pv in previews:
        if not isinstance(pv, dict):
            continue
        try:
            rev = int(pv.get("revision") or 0)
        except (TypeError, ValueError):
            continue
        if rev == revision:
            candidates.append(pv)
    if not candidates:
        return None
    cid = (kitsu_comment_id or "").strip()
    if cid:
        for pv in candidates:
            cc = pv.get("comment_id")
            if cc is not None and str(cc).strip() == cid:
                return pv
    return candidates[0]


def _kitsu_person_from_preview_or_comment(
    kitsu_task_id: str,
    revision: int,
    kitsu_comment_id: str | None,
) -> dict[str, Any]:
    """Resolve Kitsu person dict for repair using previews for ``task_id`` + ``revision``."""
    previews: list[dict[str, Any]] = []
    try:
        task = gazu.task.get_task(kitsu_task_id)
    except Exception as exc:
        log.debug("[repair_version_authors] get_task %s: %s", kitsu_task_id[:8], exc)
        task = None
    if isinstance(task, dict):
        try:
            raw = gazu.files.get_all_preview_files_for_task(task)
        except Exception as exc:
            log.debug(
                "[repair_version_authors] get_all_preview_files_for_task %s: %s",
                kitsu_task_id[:8],
                exc,
            )
            raw = None
        if raw is None:
            try:
                resp = gazu.client.get(f"data/tasks/{kitsu_task_id}/previews")
                raw = resp.json() if hasattr(resp, "json") else resp
            except Exception as exc:
                log.debug(
                    "[repair_version_authors] REST previews %s: %s",
                    kitsu_task_id[:8],
                    exc,
                )
                raw = []
        if isinstance(raw, list):
            previews = [x for x in raw if isinstance(x, dict)]
    preview = _pick_kitsu_preview_for_version_repair(
        previews, revision, kitsu_comment_id,
    )
    if preview:
        cid_for_helper = (kitsu_comment_id or "").strip()
        return _kitsu_person_dict_for_preview_uploader(preview, cid_for_helper)
    cid = (kitsu_comment_id or "").strip()
    if not cid:
        return {}
    try:
        comment = gazu.task.get_comment(cid)
    except Exception as exc:
        log.debug("[repair_version_authors] get_comment %s: %s", cid[:8], exc)
        return {}
    if not isinstance(comment, dict):
        return {}
    pid = comment.get("person_id")
    if isinstance(pid, dict):
        pid = pid.get("id")
    if not pid:
        return {}
    try:
        p = gazu.person.get_person(str(pid))
        return p if isinstance(p, dict) else {}
    except Exception as exc:
        log.debug("[repair_version_authors] get_person %s: %s", str(pid)[:8], exc)
        return {}


def repair_review_version_authors_for_paired_projects(
    processor: "KitsuProcessor",
    *,
    ayon_project_name: str | None = None,
    dry_run: bool = False,
    skip_no_ayon_login_rows: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """PATCH review version ``author`` away from processor placeholder using Kitsu breadcrumbs.

    Scans AYON ``review`` products per paired project. For each version whose ``author``
    matches ``_is_processor_placeholder_version_author``, resolves the Kitsu uploader from
    ``version.data`` (``kitsuTaskId``, ``kitsuRevision``, ``kitsuCommentId``) and calls
    ``_ensure_review_version_author_with_impersonation`` (same path as live preview sync).

    When ``dry_run`` is True, logs a structured diagnostic per candidate version and does
    not mutate AYON.

    If ``skip_no_ayon_login_rows`` is a list, each version skipped because the Kitsu
    uploader could not be mapped to an AYON login (email lookup + optional full-name
    index) is appended as a plain dict for reporting.
    """
    supports_author = ayon_update_version_supports_author_parameter()
    stats: dict[str, int] = {
        "projects": 0,
        "versions_scanned": 0,
        "placeholder_candidates": 0,
        "versions_updated": 0,
        "skipped_not_placeholder": 0,
        "skipped_no_breadcrumbs": 0,
        "skipped_no_kitsu_person": 0,
        "skipped_no_ayon_login": 0,
        "skipped_no_author_api": 0,
        "dry_run_would_update": 0,
        "errors": 0,
    }
    for pair in processor.pairing_list:
        project_id = pair.get("kitsuProjectId")
        project_name = pair.get("ayonProjectName")
        if not project_id or not project_name:
            continue
        if ayon_project_name and project_name != ayon_project_name:
            continue
        paired = processor.get_paired_ayon_project(project_id)
        if not paired or paired != project_name:
            continue
        stats["projects"] += 1
        processor_utils.set_kitsu_host(processor.kitsu_server_url)
        email_cache: dict[str, str] = {}
        fn_index = _full_name_login_index_if_enabled(processor)
        try:
            products = list(_iter_review_products_for_project(project_name))
        except Exception as exc:  # noqa: BLE001
            log.error(
                "[repair_version_authors] list review products failed project=%s: %s",
                project_name,
                exc,
            )
            stats["errors"] += 1
            continue
        product_ids = [str(p["id"]) for p in products if p.get("id")]
        if not product_ids:
            log.info("[repair_version_authors] no review products project=%s", project_name)
            continue
        try:
            versions = list(
                ayon_api.get_versions(project_name, product_ids=product_ids)
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "[repair_version_authors] get_versions failed project=%s: %s",
                project_name,
                exc,
            )
            stats["errors"] += 1
            continue

        for ver in versions:
            if not isinstance(ver, dict) or not ver.get("id"):
                continue
            stats["versions_scanned"] += 1
            version_id = str(ver["id"])
            author_val = ver.get("author")
            placeholder = _is_processor_placeholder_version_author(author_val)
            if not placeholder:
                stats["skipped_not_placeholder"] += 1
                continue
            stats["placeholder_candidates"] += 1
            data = ver.get("data") if isinstance(ver.get("data"), dict) else {}
            kitsu_task_id = str(data.get("kitsuTaskId") or "").strip()
            kitsu_comment_id = data.get("kitsuCommentId")
            kitsu_comment_str = (
                str(kitsu_comment_id).strip()
                if kitsu_comment_id not in (None, "")
                else ""
            )
            rev_raw = data.get("kitsuRevision")
            if rev_raw is None:
                rev_raw = ver.get("version")
            try:
                revision = int(rev_raw) if rev_raw is not None else 0
            except (TypeError, ValueError):
                revision = 0
            if not kitsu_task_id or revision <= 0:
                stats["skipped_no_breadcrumbs"] += 1
                log.info(
                    "[repair_version_authors] diagnostic version_id=%s placeholder=%s "
                    "kitsu_person=no breadcrumbs_missing task=%r revision=%s "
                    "ayon_login=no author_api=%s",
                    version_id[:8],
                    placeholder,
                    kitsu_task_id or None,
                    revision,
                    supports_author,
                )
                continue

            kitsu_person = _kitsu_person_from_preview_or_comment(
                kitsu_task_id,
                revision,
                kitsu_comment_str or None,
            )
            raw_pe = kitsu_person.get("email") if isinstance(kitsu_person.get("email"), str) else None
            pe = raw_pe.strip() if raw_pe and raw_pe.strip() else None
            raw_kfn = (
                kitsu_person.get("full_name")
                if isinstance(kitsu_person.get("full_name"), str)
                else None
            )
            kitsu_fn = raw_kfn.strip() if raw_kfn and raw_kfn.strip() else None
            ayon_login = _resolve_ayon_login_for_comment_sync(
                pe,
                project_name,
                email_cache,
                kitsu_full_name=kitsu_fn,
                full_name_index=fn_index,
            )
            has_person = bool(kitsu_person)
            log.info(
                "[repair_version_authors] diagnostic version_id=%s placeholder=%s "
                "kitsu_person=%s email=%s ayon_login=%s author_api=%s dry_run=%s",
                version_id[:8],
                placeholder,
                has_person,
                bool(pe),
                bool(ayon_login),
                supports_author,
                dry_run,
            )
            if not kitsu_person:
                stats["skipped_no_kitsu_person"] += 1
                continue
            if not ayon_login:
                stats["skipped_no_ayon_login"] += 1
                if skip_no_ayon_login_rows is not None:
                    kpid = kitsu_person.get("id")
                    skip_no_ayon_login_rows.append(
                        {
                            "ayon_project_name": project_name,
                            "version_id": version_id,
                            "version_subset": ver.get("subset"),
                            "product_id": ver.get("productId")
                            or ver.get("product_id"),
                            "kitsu_task_id": kitsu_task_id,
                            "kitsu_revision": revision,
                            "kitsu_comment_id": kitsu_comment_str or None,
                            "kitsu_person_id": str(kpid) if kpid else None,
                            "kitsu_email": pe,
                            "kitsu_full_name": kitsu_fn,
                            "email_lookup_key": pe.lower() if pe else None,
                            "full_name_lookup_key": (
                                _normalize_kitsu_full_name_key(kitsu_fn)
                                if kitsu_fn
                                else None
                            ),
                            "full_name_index_enabled": fn_index is not None,
                        }
                    )
                continue
            if not supports_author:
                stats["skipped_no_author_api"] += 1
                continue
            if dry_run:
                stats["dry_run_would_update"] += 1
                continue
            ver_snapshot = ver
            try:
                _ensure_review_version_author_with_impersonation(
                    processor,
                    project_name,
                    version_id,
                    ver_snapshot,
                    kitsu_person,
                    email_cache,
                    ayon_login,
                )
                updated = ayon_api.get_version_by_id(project_name, version_id)
                if not _is_processor_placeholder_version_author(updated.get("author")):
                    stats["versions_updated"] += 1
                else:
                    log.warning(
                        "[repair_version_authors] author still placeholder after patch "
                        "version_id=%s",
                        version_id[:8],
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "[repair_version_authors] patch failed version_id=%s: %s",
                    version_id[:8],
                    exc,
                )
                stats["errors"] += 1
        log.info("[repair_version_authors] finished project=%s", project_name)
    log.info("[repair_version_authors] summary %s", stats)
    return stats


def repair_kitsu_comment_activities_for_paired_projects(
    processor: "KitsuProcessor",
    *,
    ayon_project_name: str | None = None,
) -> dict[str, int]:
    """Re-run ``sync_comment_to_ayon`` for every Kitsu task comment on paired projects.

    Use after changing comment body rules to refresh or remove noise-only activities.
    Does not run structural fullsync or preview sync.
    """
    stats = {"projects": 0, "tasks_scanned": 0, "comments_synced": 0, "errors": 0}
    for pair in processor.pairing_list:
        project_id = pair.get("kitsuProjectId")
        project_name = pair.get("ayonProjectName")
        if not project_id or not project_name:
            continue
        if ayon_project_name and project_name != ayon_project_name:
            continue
        paired = processor.get_paired_ayon_project(project_id)
        if not paired or paired != project_name:
            continue
        stats["projects"] += 1
        processor_utils.set_kitsu_host(processor.kitsu_server_url)
        try:
            tasks = gazu.task.all_tasks_for_project(project_id)
        except Exception as exc:
            log.error(
                "[repair_comments] list tasks failed project=%s: %s",
                project_name,
                exc,
            )
            stats["errors"] += 1
            continue
        if not isinstance(tasks, list):
            tasks = list(tasks) if tasks else []
        log.info(
            "[repair_comments] scanning project=%s task_count=%s",
            project_name,
            len(tasks),
        )

        persons_raw = gazu.person.all_persons()
        persons_by_id = {p["id"]: p for p in persons_raw}
        statuses_raw = gazu.task.all_task_statuses()
        statuses_by_id = {s["id"]: s.get("name", "") for s in statuses_raw}

        with _content_sync_ayon_lookup_cache_scope(project_name):
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                task_id = task.get("id")
                if not task_id:
                    continue
                stats["tasks_scanned"] += 1
                try:
                    comments = gazu.task.all_comments_for_task(task)
                    if not isinstance(comments, list):
                        comments = list(comments) if comments else []
                except Exception as exc:
                    log.warning(
                        "[repair_comments] comments for task %s: %s",
                        task_id,
                        exc,
                    )
                    stats["errors"] += 1
                    continue
                log.info(
                    "[repair_comments] task %s/%s kitsu_task_id=%s comments=%s",
                    stats["tasks_scanned"],
                    len(tasks),
                    str(task_id),
                    len(comments),
                )
                for comment in comments:
                    if not isinstance(comment, dict) or not comment.get("id"):
                        continue
                    cid = str(comment["id"])
                    try:
                        sync_comment_to_ayon(
                            processor,
                            cid,
                            str(task_id),
                            str(task.get("project_id") or project_id),
                            persons_by_id=persons_by_id,
                            statuses_by_id=statuses_by_id,
                            log_progress=True,
                        )
                        stats["comments_synced"] += 1
                    except Exception as exc:
                        log.warning(
                            "[repair_comments] sync comment %s: %s",
                            cid[:8],
                            exc,
                        )
                        stats["errors"] += 1
        log.info("[repair_comments] finished project=%s", project_name)
    log.info("[repair_comments] summary %s", stats)
    return stats


def sync_all_content_for_project(
    processor: "KitsuProcessor",
    kitsu_project_id: str,
    project_name: str,
):
    """Sync all content (thumbnails, comments, previews) for a project.

    Intended to be called after the structural entity push in fullsync.
    """
    settings = processor.settings.get("sync_settings", {}).get("content_sync", {})
    if not settings.get("enabled", False):
        log.info(
            "[content_sync] Skipping project=%s: sync_settings.content_sync.enabled "
            "is false (no thumbnails, comments, previews, or concept VizDev reviewables).",
            project_name,
        )
        return

    processor_utils.set_kitsu_host(processor.kitsu_server_url)
    log.info("[content_sync] Starting content sync for project %s", project_name)

    do_thumbnails = settings.get("sync_thumbnails", True)
    do_comments = settings.get("sync_comments", True)
    do_previews = settings.get("sync_previews", True)

    with _content_sync_ayon_lookup_cache_scope(project_name):
        if do_thumbnails:
            log.info("[content_sync] Syncing entity thumbnails...")
            _sync_all_thumbnails(processor, kitsu_project_id, project_name)

        if do_comments or do_previews:
            log.info("[content_sync] Syncing task content...")
            _sync_all_task_content(
                processor,
                kitsu_project_id,
                project_name,
                do_comments=do_comments,
                do_previews=do_previews,
            )

        if do_previews:
            log.info(
                "[content_sync] Syncing concept main previews (VizDev reviewables)...",
            )
            _sync_concept_main_previews_for_project(
                processor, kitsu_project_id, project_name,
            )

    log.info("[content_sync] Content sync complete for project %s", project_name)


def sync_pinned_checklists_for_project(
    processor: "KitsuProcessor",
    kitsu_project_id: str,
    project_name: str,
) -> None:
    """Upsert AYON child tasks from Kitsu pinned checklists only (no activities).

    Requires ``checklist_subtasks.enabled`` and ``bulk_sync_after_fullsync``.
    Does not require Content sync.
    """
    if not bulk_sync_pinned_checklists_after_fullsync_enabled(processor):
        return

    paired = processor.get_paired_ayon_project(kitsu_project_id)
    if not paired or paired != project_name:
        return

    processor_utils.set_kitsu_host(processor.kitsu_server_url)
    nxtools_logging.info(
        f"[checklist_bulk] Starting pinned checklist sync for project {project_name}"
    )

    try:
        tasks = gazu.task.all_tasks_for_project(kitsu_project_id)
    except Exception as exc:
        nxtools_logging.error(
            f"[checklist_bulk] Failed to fetch tasks for {project_name}: {exc}"
        )
        return

    processed_comments = 0
    for i, task in enumerate(tasks, 1):
        task_id = task["id"]
        if i % 50 == 0:
            nxtools_logging.info(
                f"[checklist_bulk] Processing task {i}/{len(tasks)} "
                f"({processed_comments} pinned checklists so far)"
            )

        try:
            comments = gazu.task.all_comments_for_task(task)
            if not isinstance(comments, list):
                comments = list(comments) if comments else []
        except Exception as exc:
            log.warning(
                "Failed to get comments for task %s: %s", task_id, exc
            )
            continue

        for comment in comments:
            if not isinstance(comment, dict):
                continue
            if not bool(comment.get("pinned")):
                continue
            checklist = comment.get("checklist") or []
            if not isinstance(checklist, list) or len(checklist) == 0:
                continue
            comment_id = comment.get("id")
            if not comment_id:
                continue
            try:
                maybe_sync_checklist_subtasks_from_kitsu_comment(
                    processor,
                    task_id,
                    str(comment_id),
                    task.get("project_id", "") or kitsu_project_id,
                )
                processed_comments += 1
            except Exception as exc:
                log.warning(
                    "Checklist bulk sync error comment %s: %s",
                    str(comment_id)[:8],
                    exc,
                )

    nxtools_logging.info(
        f"[checklist_bulk] Pinned checklist sync complete for {project_name} "
        f"({processed_comments} pinned checklist comments processed)"
    )


def _sync_all_thumbnails(
    processor: "KitsuProcessor",
    kitsu_project_id: str,
    project_name: str,
):
    collectors = [
        gazu.asset.all_assets_for_project,
        gazu.shot.all_shots_for_project,
        gazu.shot.all_episodes_for_project,
        gazu.shot.all_sequences_for_project,
        gazu.edit.all_edits_for_project,
    ]
    for func in collectors:
        try:
            entities = func(kitsu_project_id)
        except Exception:
            continue
        for entity in entities:
            try:
                sync_thumbnail_to_ayon(
                    processor, entity, project_name,
                    kitsu_project_id=kitsu_project_id,
                )
            except Exception as exc:
                log.warning("Thumbnail sync error for %s: %s", entity.get("id"), exc)

    try:
        _cs = (processor.settings.get("sync_settings") or {}).get("concept_sync")
        concept_sync = processor_utils.normalize_concept_sync_dict(
            _cs if isinstance(_cs, dict) else None,
        )
        concepts = processor_utils.all_concepts_for_project_official_list(
            kitsu_project_id,
            concept_sync=concept_sync,
        )
    except Exception as exc:
        log.debug("Concept thumbnails skipped (no concepts API?): %s", exc)
        concepts = []
        concept_sync = None
    per_linked_thumb = processor_utils.concept_entity_model_is_per_linked_dict(
        concept_sync,
    )
    project_anchor_thumb = (
        per_linked_thumb
        and processor_utils.unlinked_concepts_anchor_is_project(concept_sync)
    )
    anchor_id = (
        str((concept_sync or {}).get("unlinked_concepts_project_kitsu_id") or "").strip()
        or processor_utils.DEFAULT_UNLINKED_PROJECT_KITSU_ID
    )
    for entity in concepts:
        if not isinstance(entity, dict):
            continue
        links_t = entity.get("entity_concept_links") or []
        if per_linked_thumb and isinstance(links_t, (list, tuple)) and links_t:
            for lid in links_t:
                leid = str(lid).strip() if lid is not None else ""
                if not leid:
                    continue
                row = dict(entity)
                row["id"] = leid
                try:
                    sync_thumbnail_to_ayon(
                        processor, row, project_name,
                        kitsu_project_id=kitsu_project_id,
                    )
                except Exception as exc:
                    log.warning(
                        "Thumbnail sync error for concept link %s: %s",
                        leid,
                        exc,
                    )
        elif project_anchor_thumb and not (
            isinstance(links_t, (list, tuple)) and links_t
        ):
            row = dict(entity)
            row["id"] = anchor_id
            try:
                sync_thumbnail_to_ayon(
                    processor, row, project_name,
                    kitsu_project_id=kitsu_project_id,
                )
            except Exception as exc:
                log.warning(
                    "Thumbnail sync error for unlinked concept -> project %s: %s",
                    entity.get("id"),
                    exc,
                )
        else:
            try:
                sync_thumbnail_to_ayon(
                    processor, entity, project_name,
                    kitsu_project_id=kitsu_project_id,
                )
            except Exception as exc:
                log.warning(
                    "Thumbnail sync error for concept %s: %s",
                    entity.get("id"),
                    exc,
                )


def _sync_concept_main_previews_for_project(
    processor: "KitsuProcessor",
    kitsu_project_id: str,
    project_name: str,
) -> None:
    """Idempotent seed: concept ``preview_file_id`` → VizDev reviewable (fullsync)."""
    try:
        _cs = (processor.settings.get("sync_settings") or {}).get("concept_sync")
        concept_sync = processor_utils.normalize_concept_sync_dict(
            _cs if isinstance(_cs, dict) else None,
        )
        concepts = processor_utils.all_concepts_for_project_official_list(
            kitsu_project_id,
            concept_sync=concept_sync,
        )
    except Exception as exc:
        log.warning("Concept preview backfill: list failed: %s", exc)
        return

    per_linked_prev = processor_utils.concept_entity_model_is_per_linked_dict(
        concept_sync,
    )
    project_anchor_prev = (
        per_linked_prev
        and processor_utils.unlinked_concepts_anchor_is_project(concept_sync)
    )
    pool_sur = concept_vizdev_surrogate_unlinked_pool()

    for c in concepts:
        if not isinstance(c, dict):
            continue
        pf = c.get("preview_file_id")
        cid = c.get("id")
        if not pf or not cid:
            continue
        pid = str(c.get("project_id") or kitsu_project_id)
        links = c.get("entity_concept_links") or []
        if per_linked_prev and isinstance(links, (list, tuple)) and links:
            for lid in links:
                leid = str(lid).strip() if lid is not None else ""
                if not leid:
                    continue
                surrogate = concept_vizdev_surrogate_for_linked_entity(leid)
                try:
                    sync_preview_to_ayon(
                        processor,
                        str(pf),
                        surrogate,
                        pid,
                        source_kitsu_concept_id=str(cid),
                    )
                except Exception as exc:
                    log.warning(
                        "Concept preview backfill failed linked=%s preview=%s: %s",
                        leid,
                        pf,
                        exc,
                    )
        elif project_anchor_prev and not (
            isinstance(links, (list, tuple)) and links
        ):
            try:
                sync_preview_to_ayon(
                    processor,
                    str(pf),
                    pool_sur,
                    pid,
                    source_kitsu_concept_id=str(cid),
                )
            except Exception as exc:
                log.warning(
                    "Concept preview backfill failed unlinked_pool preview=%s: %s",
                    pf,
                    exc,
                )
        else:
            surrogate = concept_vizdev_surrogate_kitsu_id(str(cid))
            try:
                sync_preview_to_ayon(processor, str(pf), surrogate, pid)
            except Exception as exc:
                log.warning(
                    "Concept preview backfill failed concept=%s preview=%s: %s",
                    cid,
                    pf,
                    exc,
                )


def _sync_all_task_content(
    processor: "KitsuProcessor",
    kitsu_project_id: str,
    project_name: str,
    *,
    do_comments: bool,
    do_previews: bool,
):
    try:
        tasks = gazu.task.all_tasks_for_project(kitsu_project_id)
    except Exception as exc:
        log.error("Failed to fetch tasks for content sync: %s", exc)
        return

    persons_by_id: dict[str, dict] | None = None
    statuses_by_id: dict[str, str] | None = None
    if do_comments:
        persons_raw = gazu.person.all_persons()
        persons_by_id = {p["id"]: p for p in persons_raw}
        statuses_raw = gazu.task.all_task_statuses()
        statuses_by_id = {s["id"]: s.get("name", "") for s in statuses_raw}

    task_types = processor_utils.get_task_types(kitsu_project_id)
    entity_cache: dict[str, dict | None] = {}

    for i, task in enumerate(tasks, 1):
        task_id = task["id"]
        task_row = _enrich_kitsu_task_type_name(dict(task), task_types)
        ayon_task_row = _ayon_task_by_kitsu_id(project_name, str(task_id))
        sec = cs_log.build_task_log_section(
            kitsu_api_server_url=processor.kitsu_server_url,
            kitsu_project_id=kitsu_project_id,
            project_name=project_name,
            kitsu_task=task_row,
            ayon_task=ayon_task_row,
            entity_cache=entity_cache,
        )
        sec_tok = cs_log.section_set(sec)
        try:
            if i % 20 == 0:
                cs_log.cs_log(
                    logging.INFO,
                    "[content_sync] Processing task %d/%d",
                    i,
                    len(tasks),
                )

            if do_comments:
                try:
                    comments = gazu.task.all_comments_for_task(task_row)
                    if not isinstance(comments, list):
                        comments = list(comments) if comments else []
                    comments.reverse()
                    n_comments = len(comments)
                    for j, comment in enumerate(comments, 1):
                        if j == 1 or j % 25 == 0 or j == n_comments:
                            cs_log.cs_log(
                                logging.INFO,
                                "[content_sync] comments progress %d/%d: %d/%d",
                                i,
                                len(tasks),
                                j,
                                n_comments,
                            )
                        try:
                            sync_comment_to_ayon(
                                processor,
                                comment["id"],
                                task_id,
                                task_row.get("project_id", ""),
                                persons_by_id=persons_by_id,
                                statuses_by_id=statuses_by_id,
                            )
                        except Exception as exc:
                            cid = (
                                comment.get("id", "?")
                                if isinstance(comment, dict)
                                else "?"
                            )
                            cs_log.cs_log(
                                logging.WARNING,
                                "Comment sync error %s: %s",
                                str(cid)[:8],
                                exc,
                            )
                except Exception as exc:
                    cs_log.cs_log(
                        logging.WARNING,
                        "Failed to get comments for task %s: %s",
                        task_id,
                        exc,
                    )

            if do_previews:
                try:
                    previews = gazu.files.get_all_preview_files_for_task(task_row)
                    if not isinstance(previews, list):
                        previews = list(previews) if previews else []
                    for preview in previews:
                        try:
                            sync_preview_to_ayon(
                                processor,
                                preview["id"],
                                task_id,
                                task_row.get("project_id", ""),
                            )
                        except Exception as exc:
                            cs_log.cs_log(
                                logging.WARNING,
                                "Preview sync error %s: %s",
                                preview.get("id", "?")[:8],
                                exc,
                            )
                except Exception as exc:
                    cs_log.cs_log(
                        logging.WARNING,
                        "Failed to get previews for task %s: %s",
                        task_id,
                        exc,
                    )
        finally:
            cs_log.section_reset(sec_tok)
