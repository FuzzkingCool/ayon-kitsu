"""Human-readable sync error headlines and summary enrichment for logs and AYON events."""

from __future__ import annotations

from typing import Any, Mapping


def _short_uuid(entity_id: str) -> str:
    if not entity_id or entity_id == "unknown":
        return "unknown"
    s = str(entity_id)
    if len(s) <= 13:
        return s
    return f"{s[:8]}…"


def short_reason_from_payload(payload: Mapping[str, Any] | None) -> str:
    """One-line reason for UI and summary.shortReason."""
    if not payload:
        return "unknown error"
    msg = str(payload.get("message") or "")
    low = msg.lower()
    if "timed out" in low or "timeout" in low:
        return "connection or server timeout"
    if "connection" in low and "error" in low:
        return "connection error"

    err = payload.get("error")
    detail = payload.get("detail")
    code = payload.get("code")

    if err == "unique-violation":
        if isinstance(detail, str) and detail.strip():
            d = detail.strip()
            if len(d) > 140:
                return f"duplicate record: {d[:137]}…"
            return f"duplicate record: {d}"
        return "duplicate record (unique constraint)"

    if isinstance(detail, str) and detail.strip():
        d = detail.strip()
        if len(d) > 140:
            return d[:137] + "…"
        return d

    if err:
        return str(err)
    if code is not None:
        return f"HTTP {code}"
    http_status = payload.get("httpStatus")
    if http_status is not None:
        return f"HTTP {http_status}"
    if msg:
        if len(msg) > 140:
            return msg[:137] + "…"
        return msg
    return "sync request failed"


def error_code_from_payload(payload: Mapping[str, Any] | None) -> str:
    if not payload:
        return ""
    err = payload.get("error")
    if err:
        return str(err)
    code = payload.get("code")
    if code is not None:
        return str(code)
    st = payload.get("httpStatus")
    if st is not None:
        return f"http_{st}"
    return ""


def format_entity_sync_headline(
    project_name: str,
    entity_type: str,
    entity_name: str,
    entity_id: str,
    payload: Mapping[str, Any] | None,
) -> str:
    reason = short_reason_from_payload(payload)
    sid = _short_uuid(entity_id)
    return (
        f"{project_name} | {entity_type} {entity_name} "
        f"(Kitsu {sid}): {reason}"
    )


def format_batch_push_headline(
    project_name: str,
    batch_index: int,
    batch_total: int,
    entity_type_counts: Mapping[str, int],
    payload: Mapping[str, Any] | None,
) -> str:
    reason = short_reason_from_payload(payload)
    counts = ", ".join(f"{k}:{v}" for k, v in sorted(entity_type_counts.items()))
    return (
        f"{project_name} | Batch {batch_index}/{batch_total} push failed "
        f"({counts}): {reason} — retrying entities one-by-one"
    )


def format_partial_sync_headline(
    project_name: str, failed_n: int, total: int
) -> str:
    return (
        f"{project_name} | Full sync finished with {failed_n} of {total} "
        "entities not pushed"
    )


def enrich_summary_for_emit(
    project_name: str,
    summary: Mapping[str, Any],
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge caller summary with stable keys for Event Viewer / sync-issues UI."""
    p = dict(payload or {})
    out: dict[str, Any] = {**summary}
    out.setdefault("projectName", project_name)
    out.setdefault("shortReason", short_reason_from_payload(p))
    ec = error_code_from_payload(p)
    if ec:
        out.setdefault("errorCode", ec)
    return out
