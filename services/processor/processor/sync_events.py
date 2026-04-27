"""Emit structured failures to AYON Event Viewer via ayon_api (never raises)."""

from __future__ import annotations

import os
import socket
import time
from typing import Any

import ayon_api
from ayon_api.exceptions import HTTPRequestError, ServerError
from nxtools import logging

from .sync_error_format import enrich_summary_for_emit

EVENT_TOPIC_SYNC_ENTITY_FAILED = "kitsuProcessorSyncEntityFailed"
EVENT_TOPIC_SYNC_SUMMARY = "kitsuProcessorSyncSummary"

_EMIT_MAX_ATTEMPTS = 3
_EMIT_BACKOFF_BASE_SEC = 0.15


def default_sender() -> str:
    return os.environ.get("AYON_SERVICE_NAME") or (
        f"kitsu-processor-{socket.gethostname()}"
    )


def parse_http_error_detail(exc: BaseException) -> dict[str, Any]:
    """Structured fields from an HTTP error for summary/payload."""
    out: dict[str, Any] = {"message": str(exc)}
    if isinstance(exc, ServerError):
        out["detail"] = str(exc)
        return out
    if isinstance(exc, HTTPRequestError) and exc.response is not None:
        out["httpStatus"] = getattr(exc.response, "status_code", None)
        try:
            body = exc.response.json()
            if isinstance(body, dict):
                for key in ("detail", "error", "code", "path"):
                    if key in body:
                        out[key] = body[key]
        except Exception:
            pass
    return out


def _emit_stored_event_with_retry(
    *,
    topic: str,
    project_name: str | None,
    description: str,
    summary: dict[str, Any],
    payload: dict[str, Any] | None,
    final_status: str,
    log_kind: str,
) -> None:
    """create_event + update_event with retries; logs loudly on total failure."""
    sender = default_sender()
    last_exc: BaseException | None = None
    for attempt in range(1, _EMIT_MAX_ATTEMPTS + 1):
        try:
            create_kw: dict[str, Any] = {
                "topic": topic,
                "sender": sender,
                "project_name": project_name or None,
                "description": description,
                "summary": summary,
                "finished": True,
                "store": True,
            }
            if payload is not None:
                create_kw["payload"] = payload
            event_id = ayon_api.create_event(**create_kw)
            ayon_api.update_event(
                event_id,
                sender=sender,
                project_name=project_name or None,
                status=final_status,
            )
            return
        except Exception as e:
            last_exc = e
            if attempt < _EMIT_MAX_ATTEMPTS:
                time.sleep(_EMIT_BACKOFF_BASE_SEC * attempt)
    desc_preview = description[:220] + ("…" if len(description) > 220 else "")
    logging.error(
        f"[sync_events] Failed to emit {log_kind} after {_EMIT_MAX_ATTEMPTS} attempts "
        f"(topic={topic!r} project_name={project_name!r} description={desc_preview!r}): "
        f"{last_exc!r}"
    )


def emit_sync_entity_failed(
    project_name: str,
    description: str,
    summary: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> None:
    """Create a stored event and mark it failed for Event Viewer."""
    merged_summary = enrich_summary_for_emit(project_name, summary, payload)
    _emit_stored_event_with_retry(
        topic=EVENT_TOPIC_SYNC_ENTITY_FAILED,
        project_name=project_name or None,
        description=description,
        summary=merged_summary,
        payload=payload,
        final_status="failed",
        log_kind="sync entity failed event",
    )


def emit_sync_summary(
    project_name: str,
    description: str,
    summary: dict[str, Any],
) -> None:
    """Optional partial-sync summary (warning, not necessarily failed)."""
    _emit_stored_event_with_retry(
        topic=EVENT_TOPIC_SYNC_SUMMARY,
        project_name=project_name or None,
        description=description,
        summary=summary,
        payload=None,
        final_status="finished",
        log_kind="sync summary event",
    )
