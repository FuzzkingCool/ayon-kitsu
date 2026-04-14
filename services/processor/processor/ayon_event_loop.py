# -*- coding: utf-8 -*-
"""
AYON event enrollment loop (runs in a dedicated thread).

This thread polls for AYON events (e.g. kitsu.comment_update_request) and
processes them so fast jobs stay responsive while the main thread may be
running long syncs.

Add new source_topic / handler entries to AYON_EVENT_ENROLLMENTS; each is
enrolled and processed in the same thread (one at a time).
"""

import socket
import time

import ayon_api
from nxtools import log_traceback, logging

from .checklist_kitsu_update import process_checklist_kitsu_update_request
from .comment_update import process_comment_update_request


def _get_sender() -> str:
    return f"kitsu-processor-{socket.gethostname()}"


# (source_topic, target_topic, description, max_retries, process_fn)
# process_fn(processor, src_event) -> None
# Add new AYON event enrollments here.
AYON_EVENT_ENROLLMENTS = [
    (
        "kitsu.comment_update_request",
        "addon.kitsu.processor.comment_update",
        "Update Kitsu comment with uniqueSprites",
        2,
        process_comment_update_request,
    ),
    (
        "kitsu.checklist_kitsu_update_request",
        "addon.kitsu.processor.checklist_kitsu_update",
        "Sync Kitsu checklist row from AYON task status",
        2,
        process_checklist_kitsu_update_request,
    ),
]


def run_ayon_event_loop(processor) -> None:
    """Run AYON event enrollment in a loop. Intended for a dedicated thread."""
    sender = _get_sender()
    logging.info(
        "[ayon_event_loop] Started; enrolling for: %s",
        [e[0] for e in AYON_EVENT_ENROLLMENTS],
    )
    while True:
        try:
            for (
                source_topic,
                target_topic,
                description,
                max_retries,
                process_fn,
            ) in AYON_EVENT_ENROLLMENTS:
                job = ayon_api.enroll_event_job(
                    source_topic=source_topic,
                    target_topic=target_topic,
                    sender=sender,
                    description=description,
                    max_retries=max_retries,
                )
                if not job:
                    continue
                logging.info(
                    "[ayon_event_loop] Enrolled job for %s, processing...",
                    source_topic,
                )
                src_ev = ayon_api.get_event(job["dependsOn"])
                project_name = src_ev.get("project") or ""
                ayon_api.update_event(
                    job["id"],
                    sender=sender,
                    status="in_progress",
                    project_name=project_name,
                    description=description + "...",
                )
                try:
                    process_fn(processor, src_ev)
                except Exception:
                    log_traceback(f"AYON event {source_topic} error")
                    ayon_api.update_event(
                        job["id"],
                        sender=sender,
                        status="failed",
                        project_name=project_name,
                        description=description + " failed",
                    )
                else:
                    ayon_api.update_event(
                        job["id"],
                        sender=sender,
                        status="finished",
                        project_name=project_name,
                        description=description + " done",
                    )
                break
            else:
                time.sleep(2)
        except Exception as e:
            logging.error("[ayon_event_loop] Loop error: %s", e)
            log_traceback("ayon_event_loop")
            time.sleep(5)
