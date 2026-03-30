"""Emit structured failures to AYON Event Viewer via ayon_api (never raises)."""

from __future__ import annotations

import os
import socket
from typing import Any

import ayon_api
from ayon_api.exceptions import HTTPRequestError, ServerError
from nxtools import logging

from .sync_error_format import enrich_summary_for_emit

EVENT_TOPIC_SYNC_ENTITY_FAILED = "kitsuProcessorSyncEntityFailed"
EVENT_TOPIC_SYNC_SUMMARY = "kitsuProcessorSyncSummary"


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


def emit_sync_entity_failed(
    project_name: str,
    description: str,
    summary: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> None:
    """Create a stored event and mark it failed for Event Viewer."""
    sender = default_sender()
    merged_summary = enrich_summary_for_emit(project_name, summary, payload)
    try:
        event_id = ayon_api.create_event(
            topic=EVENT_TOPIC_SYNC_ENTITY_FAILED,
            sender=sender,
            project_name=project_name or None,
            description=description,
            summary=merged_summary,
            payload=payload,
            finished=True,
            store=True,
        )
        ayon_api.update_event(
            event_id,
            sender=sender,
            project_name=project_name or None,
            status="failed",
        )
    except Exception as e:
        logging.error(f"[sync_events] Failed to emit AYON event: {e}")


def emit_sync_summary(
    project_name: str,
    description: str,
    summary: dict[str, Any],
) -> None:
    """Optional partial-sync summary (warning, not necessarily failed)."""
    sender = default_sender()
    try:
        event_id = ayon_api.create_event(
            topic=EVENT_TOPIC_SYNC_SUMMARY,
            sender=sender,
            project_name=project_name or None,
            description=description,
            summary=summary,
            finished=True,
            store=True,
        )
        ayon_api.update_event(
            event_id,
            sender=sender,
            project_name=project_name or None,
            status="finished",
        )
    except Exception as e:
        logging.error(f"[sync_events] Failed to emit summary event: {e}")
