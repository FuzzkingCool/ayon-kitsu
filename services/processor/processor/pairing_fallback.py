"""Build Kitsu↔AYON pairing using the processor's Kitsu session + AYON API.

``GET /addons/kitsu/.../pairing`` runs on the AYON server and logs into Kitsu
using **studio** secret references. When that login fails but the processor has
already authenticated to Kitsu (e.g. via ``KITSU_*`` env), this module reproduces
the same pairing row shape as the addon endpoint.
"""

from __future__ import annotations

from typing import Any

import ayon_api
import gazu
from nxtools import logging


def pairing_http_error_is_kitsu_login(detail: object) -> bool:
    """True when AYON serialized a ``KitsuLoginException`` from the addon."""
    text = str(detail or "").lower()
    compact = text.replace("_", "")
    return "kitsuloginexception" in compact or (
        "could not login to kitsu" in text and "invalid credentials" in text
    )


def _ayon_projects_kitsu_index() -> dict[str, str] | None:
    """Map Kitsu project id -> AYON project name from ``GET /api/projects``."""
    try:
        res = ayon_api.get("/api/projects")
    except Exception as exc:
        logging.warning(
            f"[pairing_fallback] AYON GET /api/projects failed: {exc}"
        )
        return None
    if res.status_code != 200:
        detail = getattr(res, "detail", "")
        logging.warning(
            f"[pairing_fallback] AYON GET /api/projects "
            f"status={res.status_code} detail={detail}"
        )
        return None
    body = res.data
    projects: list[Any]
    if isinstance(body, list):
        projects = body
    elif isinstance(body, dict):
        projects = body.get("projects") or []
        if not isinstance(projects, list):
            projects = []
    else:
        projects = []

    out: dict[str, str] = {}
    for p in projects:
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        data = p.get("data") or {}
        kid = data.get("kitsuProjectId")
        if kid and name:
            out[str(kid)] = str(name)
    return out


def pairing_list_from_processor_session() -> list[dict[str, Any]] | None:
    """Return pairing rows like ``GET .../pairing``, or ``None`` if either side fails."""
    try:
        raw = gazu.client.get("data/projects")
    except Exception as exc:
        logging.warning(
            f"[pairing_fallback] Kitsu GET data/projects failed: {exc}"
        )
        return None
    if not isinstance(raw, list):
        logging.warning(
            f"[pairing_fallback] Kitsu data/projects returned non-list: "
            f"{type(raw).__name__!r}"
        )
        return None

    ayon_by_kitsu = _ayon_projects_kitsu_index()
    if ayon_by_kitsu is None:
        return None

    rows: list[dict[str, Any]] = []
    for project in raw:
        if not isinstance(project, dict):
            continue
        pid = project.get("id")
        if not pid:
            continue
        pid_s = str(pid)
        rows.append(
            {
                "kitsuProjectId": pid_s,
                "kitsuProjectName": project.get("name") or "",
                "kitsuProjectCode": project.get("code"),
                "ayonProjectName": ayon_by_kitsu.get(pid_s),
            }
        )
    return rows
