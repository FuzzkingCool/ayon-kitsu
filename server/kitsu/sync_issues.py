"""Build sync issue payloads for GET /processor/sync-issues."""

from __future__ import annotations

from typing import Any


def dedupe_sync_issue_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep newest row per (project, Kitsu entity id, error code) when ids exist."""
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        summary = row.get("summary")
        if not isinstance(summary, dict):
            summary = {}
        kid = summary.get("kitsuEntityId") or summary.get("kitsuTaskId")
        if not kid:
            out.append(row)
            continue
        key = (
            row.get("project_name"),
            str(kid),
            str(summary.get("errorCode", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def issue_row_to_dict(row: Any) -> dict[str, Any]:
    """Normalize a Postgres events row for JSON."""
    return {
        "id": str(row["id"]) if row.get("id") is not None else None,
        "topic": row["topic"],
        "description": row["description"],
        "project_name": row.get("project_name"),
        "status": row["status"],
        "summary": row["summary"],
        "payload": row.get("payload"),
        "created_at": row["created_at"].isoformat()
        if row.get("created_at")
        else None,
        "updated_at": row["updated_at"].isoformat()
        if row.get("updated_at")
        else None,
    }
