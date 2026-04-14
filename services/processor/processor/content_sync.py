"""Incremental content sync: Kitsu comments/previews/thumbnails -> AYON.

Used by the processor service for both Socket.IO-driven incremental sync
and full-sync content pass.  All heavy lifting (download, upload) happens
inline so the processor can run in a container with no local disk state.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import ayon_api
import gazu
from nxtools import logging as nxtools_logging

from . import utils as processor_utils
from .checklist_subtask_sync import (
    bulk_sync_pinned_checklists_after_fullsync_enabled,
    delete_checklist_subtasks_for_comment,
    maybe_sync_checklist_subtasks_from_kitsu_comment,
)

if TYPE_CHECKING:
    from .processor import KitsuProcessor

log = logging.getLogger("content_sync")


# ---------------------------------------------------------------------------
# ID resolution helpers
# ---------------------------------------------------------------------------

def _ayon_folder_by_kitsu_id(project_name: str, kitsu_id: str) -> dict | None:
    for folder in ayon_api.get_folders(project_name):
        if (folder.get("data") or {}).get("kitsuId") == kitsu_id:
            return folder
    return None


def _ayon_task_by_kitsu_id(project_name: str, kitsu_id: str) -> dict | None:
    for task in ayon_api.get_tasks(project_name):
        if (task.get("data") or {}).get("kitsuId") == kitsu_id:
            return task
    return None


def _preview_download_url(preview: dict) -> str:
    ext = preview.get("extension", "png")
    pid = preview["id"]
    prefix = "movies" if ext == "mp4" else "pictures"
    return f"{prefix}/originals/preview-files/{pid}.{ext}"


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

def _find_or_create_review_product(
    project_name: str, folder_id: str, task_type_name: str, kitsu_task_id: str,
) -> str:
    product_name = f"{task_type_name.lower()}KitsuReview"
    for prod in ayon_api.get_products(project_name, folder_ids=[folder_id]):
        if prod["name"] == product_name:
            return prod["id"]
    return ayon_api.create_product(
        project_name,
        name=product_name,
        product_type="review",
        folder_id=folder_id,
        data={"kitsuTaskId": kitsu_task_id},
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
):
    """Download a single Kitsu preview file and upload as AYON reviewable."""
    project_name = processor.get_paired_ayon_project(project_id)
    if not project_name:
        return

    processor_utils.set_kitsu_host(processor.kitsu_server_url)

    try:
        preview = gazu.files.get_preview_file(preview_file_id)
    except Exception as exc:
        log.error("Failed to get preview file %s: %s", preview_file_id, exc)
        return

    try:
        task = gazu.task.get_task(task_id)
    except Exception as exc:
        log.error("Failed to get Kitsu task %s: %s", task_id, exc)
        return

    entity_id = task.get("entity_id", "")
    ayon_folder = _ayon_folder_by_kitsu_id(project_name, entity_id)
    if not ayon_folder:
        log.debug("No AYON folder for entity %s", entity_id)
        return

    ayon_task = _ayon_task_by_kitsu_id(project_name, task_id)
    ayon_task_id = ayon_task["id"] if ayon_task else None

    task_type = task.get("task_type_name", task.get("task_type_id", "unknown"))
    revision = preview.get("revision", 1)
    comment_id = preview.get("comment_id", "")

    product_id = _find_or_create_review_product(
        project_name, ayon_folder["id"], task_type, task_id,
    )
    version_id = _find_or_create_review_version(
        project_name, product_id, revision, task_id, comment_id, ayon_task_id,
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
    try:
        gazu.client.download(url, tmp_path)
        ayon_api.upload_reviewable(
            project_name, version_id, tmp_path, label=original_name,
        )
        main_ok = True
        log.info("Reviewable uploaded: %s -> version %s", original_name, version_id)
    except Exception as exc:
        log.error("Reviewable upload failed for %s: %s", preview_file_id, exc)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if not main_ok:
        return

    pos = int(preview.get("position") or 0)
    base = f"{pos:02d}_{preview_file_id}"
    ann_tmp: str | None = None
    meta_tmp: str | None = None
    try:
        ann_payload = {
            "preview_file_id": preview_file_id,
            "annotations": preview.get("annotations")
            if preview.get("annotations") is not None
            else [],
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8",
        ) as ann_f:
            json.dump(ann_payload, ann_f, default=str)
            ann_tmp = ann_f.name
        ayon_api.upload_reviewable(
            project_name, version_id, ann_tmp, label=f"{base}_annotations.json",
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8",
        ) as meta_f:
            json.dump(preview, meta_f, default=str)
            meta_tmp = meta_f.name
        ayon_api.upload_reviewable(
            project_name, version_id, meta_tmp, label=f"{base}_preview_file.json",
        )
        log.debug("Preview sidecar reviewables uploaded for %s", preview_file_id)
    except Exception as exc:
        log.warning("Preview sidecar upload failed for %s: %s", preview_file_id, exc)
    finally:
        if ann_tmp:
            Path(ann_tmp).unlink(missing_ok=True)
        if meta_tmp:
            Path(meta_tmp).unlink(missing_ok=True)

    _merge_kitsu_preview_file_ids_on_version(project_name, version_id, [preview_file_id])


# ---------------------------------------------------------------------------
# Comment sync
# ---------------------------------------------------------------------------

def _build_comment_body(comment: dict, persons: dict[str, dict], statuses: dict[str, str]) -> str:
    person = persons.get(comment.get("person_id", ""), {})
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
            chk = "x" if item.get("checked") else " "
            parts.append(f"- [{chk}] {item.get('text', '')}")

    previews = comment.get("previews") or []
    if previews:
        sorted_prev = sorted(previews, key=lambda p: p.get("position", 0))
        rev = sorted_prev[0].get("revision", "?")
        parts.append("")
        parts.append("---")
        parts.append(f"**Revision {rev}** ({len(sorted_prev)} review files)")
        for p in sorted_prev:
            parts.append(f"- `{p.get('original_name', p['id'])}`")

    return "\n".join(parts) + "\n"


def sync_comment_to_ayon(
    processor: "KitsuProcessor",
    comment_id: str,
    task_id: str,
    project_id: str,
):
    """Sync a single Kitsu comment to AYON as an activity."""
    project_name = processor.get_paired_ayon_project(project_id)
    if not project_name:
        return

    processor_utils.set_kitsu_host(processor.kitsu_server_url)

    try:
        comment = gazu.task.get_comment(comment_id)
    except Exception as exc:
        log.error("Failed to get comment %s: %s", comment_id, exc)
        return

    ayon_task = _ayon_task_by_kitsu_id(project_name, task_id)
    if not ayon_task:
        log.debug("No AYON task for Kitsu task %s", task_id)
        return
    ayon_task_id = ayon_task["id"]

    existing = list(ayon_api.get_activities(
        project_name, entity_ids=[ayon_task_id], activity_types=["comment"],
    ))
    for act in existing:
        if _activity_kitsu_comment_id(act) == str(comment_id):
            log.debug(
                "[content_sync] action=skip_duplicate "
                "kitsu_comment_id=%s activity_id=%s reason=kitsuCommentId",
                comment_id, act.get("activityId"),
            )
            maybe_sync_checklist_subtasks_from_kitsu_comment(
                processor, task_id, comment_id, project_id,
            )
            return

    persons_raw = gazu.person.all_persons()
    persons = {p["id"]: p for p in persons_raw}
    statuses_raw = gazu.task.all_task_statuses()
    statuses = {s["id"]: s.get("name", "") for s in statuses_raw}

    body = _build_comment_body(comment, persons, statuses)
    person = persons.get(comment.get("person_id", ""), {})
    author_name = person.get("full_name", "Unknown")
    status_name = statuses.get(comment.get("task_status_id", ""), "")

    file_ids: list[str] = []
    attachments = comment.get("attachment_files") or []
    for att in attachments:
        att_name = att.get("name", att["id"])
        with tempfile.NamedTemporaryFile(suffix=f".{att.get('extension', 'bin')}", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            gazu.task.download_attachment_file(att, tmp_path)
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
    previews = comment.get("previews") or []
    for pv in sorted(previews, key=lambda x: x.get("position", 0)):
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

    data: dict[str, Any] = {
        "kitsuCommentId": comment_id,
        "kitsuAuthor": author_name,
    }
    if status_name:
        data["kitsuStatusChange"] = status_name
    if comment.get("pinned"):
        data["kitsuPinned"] = True

    try:
        aid = ayon_api.create_activity(
            project_name,
            entity_id=ayon_task_id,
            entity_type="task",
            activity_type="comment",
            body=body,
            file_ids=file_ids if file_ids else None,
            timestamp=comment.get("created_at"),
            data=data,
        )
        log.info("Activity created %s for comment %s", aid, comment_id[:8])
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
    """Delete the AYON activity that corresponds to a deleted Kitsu comment."""
    project_name = processor.get_paired_ayon_project(project_id)
    if not project_name:
        return

    ayon_task = _ayon_task_by_kitsu_id(project_name, task_id)
    if not ayon_task:
        return

    delete_checklist_subtasks_for_comment(
        processor, project_name, comment_id, task_id,
    )

    for act in ayon_api.get_activities(
        project_name, entity_ids=[ayon_task["id"]], activity_types=["comment"],
    ):
        if _activity_kitsu_comment_id(act) == str(comment_id):
            try:
                ayon_api.delete_activity(project_name, act["activityId"])
                log.info("Deleted activity %s for comment %s", act["activityId"], comment_id[:8])
            except Exception as exc:
                log.error("Failed to delete activity for comment %s: %s", comment_id[:8], exc)
            return


def update_comment_on_ayon(
    processor: "KitsuProcessor",
    comment_id: str,
    task_id: str,
    project_id: str,
):
    """Update the AYON activity body for an edited Kitsu comment."""
    project_name = processor.get_paired_ayon_project(project_id)
    if not project_name:
        return

    processor_utils.set_kitsu_host(processor.kitsu_server_url)

    try:
        comment = gazu.task.get_comment(comment_id)
    except Exception as exc:
        log.error("Failed to get comment %s: %s", comment_id, exc)
        return

    ayon_task = _ayon_task_by_kitsu_id(project_name, task_id)
    if not ayon_task:
        return

    persons_raw = gazu.person.all_persons()
    persons = {p["id"]: p for p in persons_raw}
    statuses_raw = gazu.task.all_task_statuses()
    statuses = {s["id"]: s.get("name", "") for s in statuses_raw}

    body = _build_comment_body(comment, persons, statuses)

    for act in ayon_api.get_activities(
        project_name, entity_ids=[ayon_task["id"]], activity_types=["comment"],
    ):
        if _activity_kitsu_comment_id(act) == str(comment_id):
            try:
                ayon_api.update_activity(project_name, act["activityId"], body=body)
                log.info("Updated activity %s for comment %s", act["activityId"], comment_id[:8])
            except Exception as exc:
                log.error("Failed to update activity for comment %s: %s", comment_id[:8], exc)
            maybe_sync_checklist_subtasks_from_kitsu_comment(
                processor, task_id, comment_id, project_id,
            )
            return

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
                        log.warning("Comment sync error %s: %s", comment.get("id", "?")[:8], exc)
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
