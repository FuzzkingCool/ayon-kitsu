"""Incremental content sync: Kitsu comments/previews/thumbnails -> AYON.

Used by the processor service for both Socket.IO-driven incremental sync
and full-sync content pass.  All heavy lifting (download, upload) happens
inline so the processor can run in a container with no local disk state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import ayon_api
import gazu
import requests
from ayon_api.exceptions import HTTPRequestError
from nxtools import logging as nxtools_logging, slugify

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

def _is_raster_image_extension(ext: str | None) -> bool:
    if not ext:
        return False
    return ext.lower().lstrip(".") in (
        "png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff",
    )


def sync_thumbnail_to_ayon(
    processor: "KitsuProcessor",
    entity: dict,
    project_name: str,
):
    """Download Kitsu entity preview (original image when possible) and set AYON folder thumbnail."""
    preview_file_id = entity.get("preview_file_id")
    if not preview_file_id:
        return

    kitsu_id = entity["id"]
    ayon_folder = _ayon_folder_by_kitsu_id(project_name, kitsu_id)
    if not ayon_folder:
        return

    existing_thumb_kid = (ayon_folder.get("data") or {}).get("kitsuThumbnailPreviewId")
    if str(existing_thumb_kid) == str(preview_file_id):
        log.debug(
            "[content_sync] action=skip_duplicate "
            "kitsu_thumbnail_preview_id=%s folder_id=%s reason=kitsuThumbnailPreviewId",
            preview_file_id, ayon_folder.get("id"),
        )
        return

    try:
        pf = gazu.files.get_preview_file(preview_file_id)
    except Exception as exc:
        log.warning("get_preview_file failed for entity %s: %s", kitsu_id, exc)
        return

    if not pf:
        return

    st = (pf.get("status") or "").lower()
    if st and st != "ready":
        log.debug("Entity preview %s not ready (%s), skipping thumbnail sync", preview_file_id, st)
        return

    ext = (pf.get("extension") or "png").lstrip(".")
    if _is_raster_image_extension(ext):
        url = gazu.files.get_preview_file_url(pf)
        suffix = f".{ext}"
    else:
        url = f"pictures/thumbnails/preview-files/{preview_file_id}.png"
        suffix = ".png"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = tmp.name

    try:
        gazu.client.download(url, tmp_path)
        thumb_id = ayon_api.create_thumbnail(project_name, tmp_path)
        ayon_api.update_folder(
            project_name, ayon_folder["id"],
            thumbnail_id=thumb_id,
            data={**(ayon_folder.get("data") or {}), "kitsuThumbnailPreviewId": preview_file_id},
        )
        log.info("Thumbnail synced for entity %s -> folder %s", kitsu_id, ayon_folder["id"])
    except Exception as exc:
        log.warning("Thumbnail sync failed for %s: %s", kitsu_id, exc)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


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


def _find_or_create_review_version(
    project_name: str, product_id: str, revision: int,
    kitsu_task_id: str, kitsu_comment_id: str,
    ayon_task_id: str | None,
) -> str:
    for ver in ayon_api.get_versions(project_name, product_ids=[product_id]):
        if ver.get("version") == revision:
            return ver["id"]
    return ayon_api.create_version(
        project_name,
        version=revision,
        product_id=product_id,
        task_id=ayon_task_id,
        data={
            "kitsuRevision": revision,
            "kitsuCommentId": kitsu_comment_id,
            "kitsuTaskId": kitsu_task_id,
        },
    )


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
) -> None:
    for act in ayon_api.get_activities(
        project_name, entity_ids=[ayon_task_id], activity_types=["comment"],
    ):
        if _activity_kitsu_comment_id(act) != str(kitsu_comment_id):
            continue
        try:
            ayon_api.delete_activity(project_name, act["activityId"])
            log.info(
                "Deleted activity %s for comment %s",
                act.get("activityId"),
                kitsu_comment_id[:8],
            )
        except Exception as exc:
            log.error(
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
    """
    project_name = processor.get_paired_ayon_project(project_id)
    if not project_name:
        return

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
            log.error("Failed to get Kitsu task %s: %s", resolved_task_id, exc)
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

    if is_concept_media_path and ayon_task is None:
        log.warning(
            "[content_sync] no AYON VizDev task for concept %s; "
            "skip preview %s (push concept first)",
            (entity_id or "")[:8],
            preview_file_id,
        )
        return

    ayon_task_id = ayon_task["id"] if ayon_task else None

    revision = preview.get("revision", 1)
    comment_id = preview.get("comment_id", "")

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
    version_id = _find_or_create_review_version(
        project_name, product_id, revision, resolved_task_id, comment_id, ayon_task_id,
    )

    ver_snapshot = ayon_api.get_version_by_id(project_name, version_id)
    if _preview_file_id_in_version_data(preview_file_id, ver_snapshot):
        log.info(
            "[content_sync] action=skip_duplicate "
            "kitsu_preview_file_id=%s version_id=%s reason=kitsuPreviewFileIds",
            preview_file_id, version_id,
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
                log.info(
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
                    log.warning(
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
                            log.info(
                                "[content_sync] preview_media_project_file "
                                "preview_file_id=%s project_file_id=%s filename=%r "
                                "reason=reviewable_extract_fallback",
                                preview_file_id,
                                fid,
                                upload_name,
                            )
                        else:
                            log.warning(
                                "[content_sync] preview_extract_fallback_no_file_id "
                                "preview_file_id=%s",
                                preview_file_id,
                            )
                    except Exception as pf_exc:
                        log.error(
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
                log.info(
                    "[content_sync] preview_pdf_uploaded preview_file_id=%s "
                    "project_file_id=%s version_id=%s filename=%r",
                    preview_file_id,
                    fid,
                    version_id,
                    pdf_filename,
                )
            else:
                log.warning(
                    "[content_sync] preview_pdf_upload_no_file_id preview_file_id=%s",
                    preview_file_id,
                )
        else:
            log.warning(
                "[content_sync] skip preview upload preview_file_id=%s "
                "original_name=%r kitsu_extension=%r size=%s head16_hex=%s "
                "reason=unsupported_or_unknown_media_type",
                preview_file_id,
                original_name,
                ext,
                sz,
                head_hex or None,
            )
    except Exception as exc:
        log.error(
            "Reviewable upload failed for %s: %s%s",
            preview_file_id,
            exc,
            _http_error_body_snippet(exc),
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if not main_ok:
        return

    ann_payload = (
        preview.get("annotations") if preview.get("annotations") is not None else []
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

    _merge_kitsu_preview_file_ids_on_version(project_name, version_id, [preview_file_id])


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


def _build_comment_body(comment: dict, persons: dict[str, dict], statuses: dict[str, str]) -> str:
    person = persons.get(comment.get("person_id", ""), {})
    if not isinstance(person, dict):
        person = {}
    author = person.get("full_name", "Unknown")
    status_name = statuses.get(comment.get("task_status_id", ""), "")

    parts = [f"**[{author}]**"]
    if status_name:
        parts[0] += f" -- _{status_name}_"
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
    if preview_rows:
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
):
    """Sync a single Kitsu comment to AYON as one or more comment activities (multipart)."""
    project_name = processor.get_paired_ayon_project(project_id)
    if not project_name:
        return

    processor_utils.set_kitsu_host(processor.kitsu_server_url)

    try:
        comment = gazu.task.get_comment(comment_id)
    except Exception as exc:
        log.error("Failed to get comment %s: %s", comment_id, exc)
        return

    if not isinstance(comment, dict):
        log.warning(
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

    persons_raw = gazu.person.all_persons()
    persons = {p["id"]: p for p in persons_raw}
    statuses_raw = gazu.task.all_task_statuses()
    statuses = {s["id"]: s.get("name", "") for s in statuses_raw}

    body = _build_comment_body(comment, persons, statuses)
    person = persons.get(comment.get("person_id", ""), {})
    if not isinstance(person, dict):
        person = {}
    author_name = person.get("full_name", "Unknown")
    status_name = statuses.get(comment.get("task_status_id", ""), "")

    file_ids: list[str] = []
    attachments = comment.get("attachment_files") or []
    for att in attachments:
        if not isinstance(att, dict):
            log.debug("Skipping non-dict attachment on comment %s", comment_id[:8])
            continue
        att_name = att.get("name", att["id"])
        with tempfile.NamedTemporaryFile(suffix=f".{att.get('extension', 'bin')}", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            gazu.files.download_attachment_file(att, tmp_path)
            resp = ayon_api.upload_project_file(
                project_name, tmp_path, filename=att_name,
            )
            resp_data = resp.json() if hasattr(resp, "json") else {}
            fid = resp_data.get("id", "")
            if fid:
                file_ids.append(fid)
        except Exception as exc:
            log.warning("Attachment upload failed %s: %s", att.get("id"), exc)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    uploaded_preview_sidecars = False
    preview_rows = _normalize_kitsu_preview_entries(comment.get("previews") or [])
    for pv in sorted(
        preview_rows,
        key=lambda x: int(x.get("position") or 0),
    ):
        pid = pv["id"]
        pos = int(pv.get("position") or 0)
        base = f"{pos:02d}_{pid}"
        try:
            pfile = gazu.files.get_preview_file(pid)
        except Exception as exc:
            log.warning("get_preview_file %s for comment sidecar: %s", pid, exc)
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
                resp = ayon_api.upload_project_file(
                    project_name, pj_tmp, filename=fname,
                )
                resp_data = resp.json() if hasattr(resp, "json") else {}
                fid = resp_data.get("id", "")
                if fid:
                    file_ids.append(fid)
                    uploaded_preview_sidecars = True
            except Exception as exc:
                log.warning("Comment preview sidecar upload failed %s: %s", fname, exc)
            finally:
                if pj_tmp:
                    Path(pj_tmp).unlink(missing_ok=True)

    if uploaded_preview_sidecars:
        body = (
            body.rstrip()
            + "\n\n_Kitsu annotation and preview metadata JSON files are attached._\n"
        )

    full_body = body
    body_sha256 = _sha256_utf8(full_body)
    part_bodies = _split_comment_body_for_ayon_activities(full_body)
    part_count = len(part_bodies)

    existing = list(ayon_api.get_activities(
        project_name, entity_ids=[ayon_task_id], activity_types=["comment"],
    ))
    matches = _activities_for_kitsu_comment_id(existing, comment_id)
    if _existing_comment_sync_uptodate(matches, part_count, body_sha256, full_body):
        log.debug(
            "[content_sync] action=skip_duplicate "
            "kitsu_comment_id=%s parts=%s reason=hash_and_parts",
            comment_id,
            part_count,
        )
        maybe_sync_checklist_subtasks_from_kitsu_comment(
            processor, task_id, comment_id, project_id,
        )
        return

    _delete_all_activities_for_kitsu_comment(project_name, ayon_task_id, comment_id)

    base_data: dict[str, Any] = {
        "kitsuCommentId": comment_id,
        "kitsuAuthor": author_name,
        "kitsuCommentBodySha256": body_sha256,
    }
    if status_name:
        base_data["kitsuStatusChange"] = status_name
    if comment.get("pinned"):
        base_data["kitsuPinned"] = True

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
            log.info(
                "Activity created %s for comment %s part %s/%s",
                aid,
                comment_id[:8],
                part_index,
                part_count,
            )
    except Exception as exc:
        log.error("Activity creation failed for comment %s: %s", comment_id[:8], exc)
    finally:
        maybe_sync_checklist_subtasks_from_kitsu_comment(
            processor, task_id, comment_id, project_id,
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
    sync_comment_to_ayon(processor, comment_id, task_id, project_id)


# ---------------------------------------------------------------------------
# Full-project content sync (called from fullsync.py)
# ---------------------------------------------------------------------------

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

    if do_thumbnails:
        log.info("[content_sync] Syncing entity thumbnails...")
        _sync_all_thumbnails(processor, kitsu_project_id, project_name)

    if do_comments or do_previews:
        log.info("[content_sync] Syncing task content...")
        _sync_all_task_content(processor, kitsu_project_id, project_name,
                               do_comments=do_comments, do_previews=do_previews)

    if do_previews:
        log.info("[content_sync] Syncing concept main previews (VizDev reviewables)...")
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
                sync_thumbnail_to_ayon(processor, entity, project_name)
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
                    sync_thumbnail_to_ayon(processor, row, project_name)
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
                sync_thumbnail_to_ayon(processor, row, project_name)
            except Exception as exc:
                log.warning(
                    "Thumbnail sync error for unlinked concept -> project %s: %s",
                    entity.get("id"),
                    exc,
                )
        else:
            try:
                sync_thumbnail_to_ayon(processor, entity, project_name)
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

    persons_raw = gazu.person.all_persons()
    persons = {p["id"]: p for p in persons_raw}
    statuses_raw = gazu.task.all_task_statuses()
    statuses = {s["id"]: s.get("name", "") for s in statuses_raw}

    for i, task in enumerate(tasks, 1):
        task_id = task["id"]
        if i % 20 == 0:
            log.info("[content_sync] Processing task %d/%d", i, len(tasks))

        if do_comments:
            try:
                comments = gazu.task.all_comments_for_task(task)
                if not isinstance(comments, list):
                    comments = list(comments) if comments else []
                comments.reverse()
                for comment in comments:
                    try:
                        sync_comment_to_ayon(
                            processor, comment["id"], task_id, task.get("project_id", ""),
                        )
                    except Exception as exc:
                        cid = (
                            comment.get("id", "?")
                            if isinstance(comment, dict)
                            else "?"
                        )
                        log.warning(
                            "Comment sync error %s: %s",
                            str(cid)[:8],
                            exc,
                        )
            except Exception as exc:
                log.warning("Failed to get comments for task %s: %s", task_id, exc)

        if do_previews:
            try:
                previews = gazu.files.get_all_preview_files_for_task(task)
                if not isinstance(previews, list):
                    previews = list(previews) if previews else []
                for preview in previews:
                    try:
                        sync_preview_to_ayon(
                            processor, preview["id"], task_id, task.get("project_id", ""),
                        )
                    except Exception as exc:
                        log.warning("Preview sync error %s: %s", preview.get("id", "?")[:8], exc)
            except Exception as exc:
                log.warning("Failed to get previews for task %s: %s", task_id, exc)
