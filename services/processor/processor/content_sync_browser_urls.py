"""Pure URL builders for content_sync logging (Kitsu SPA + AYON web app paths).

AYON web (ynput/ayon-frontend) deep-links Task progress / Workfiles via the
Details panel query contract: ``project``, ``type``, ``id``. Do **not** emit
legacy ``?task=…&folder=…`` — the SPA does not hydrate selection from those
keys (see ``useDetailsPanelURLSync`` / ``DetailsPanelContext``).
"""

from __future__ import annotations

import re
import urllib.parse

# Kitsu web UI: same shot/asset split as client launcher_open_in_kitsu.
_KITSU_SHOTS_FOLDER_TYPES = frozenset({"Shots", "Sequence", "Shot"})


def kitsu_ui_base_url(kitsu_api_server_url: str) -> str:
    """Strip trailing ``/api`` from Kitsu REST host so links open the browser UI."""
    base = (kitsu_api_server_url or "").rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")].rstrip("/")
    return base


def kitsu_browser_path_for_task(
    kitsu_project_id: str,
    *,
    kitsu_task_id: str,
    folder_type_for_entity: str | None,
) -> str:
    """Path under UI base, e.g. ``/productions/{pid}/shots/tasks/{tid}``."""
    pid = urllib.parse.quote(str(kitsu_project_id).strip(), safe="")
    tid = urllib.parse.quote(str(kitsu_task_id).strip(), safe="")
    kt = (
        "shots"
        if (folder_type_for_entity or "") in _KITSU_SHOTS_FOLDER_TYPES
        else "assets"
    )
    return f"/productions/{pid}/{kt}/tasks/{tid}"


def kitsu_browser_path_for_entity(
    kitsu_project_id: str,
    *,
    kitsu_entity_id: str,
    folder_type_for_entity: str | None,
) -> str:
    """Path to entity (no task), e.g. ``/productions/{pid}/shots/{eid}``."""
    pid = urllib.parse.quote(str(kitsu_project_id).strip(), safe="")
    eid = urllib.parse.quote(str(kitsu_entity_id).strip(), safe="")
    kt = (
        "shots"
        if (folder_type_for_entity or "") in _KITSU_SHOTS_FOLDER_TYPES
        else "assets"
    )
    return f"/productions/{pid}/{kt}/{eid}"


def kitsu_browser_url(
    kitsu_api_server_url: str,
    kitsu_project_id: str,
    *,
    kitsu_task_id: str | None = None,
    kitsu_entity_id: str | None = None,
    folder_type_for_entity: str | None = None,
) -> str | None:
    """Full Kitsu browser URL, or None if inputs are unusable."""
    base = kitsu_ui_base_url(kitsu_api_server_url)
    if not base or not str(kitsu_project_id).strip():
        return None
    tid = str(kitsu_task_id or "").strip()
    eid = str(kitsu_entity_id or "").strip()
    if tid:
        path = kitsu_browser_path_for_task(
            kitsu_project_id,
            kitsu_task_id=tid,
            folder_type_for_entity=folder_type_for_entity,
        )
    elif eid:
        path = kitsu_browser_path_for_entity(
            kitsu_project_id,
            kitsu_entity_id=eid,
            folder_type_for_entity=folder_type_for_entity,
        )
    else:
        return None
    return f"{base}{path}"


def _encode_project_segment(project_name: str) -> str:
    """Encode a single path segment for ``/projects/{segment}/…``."""
    return urllib.parse.quote(str(project_name).strip(), safe="")


def ayon_browser_url_project_overview(ayon_base_url: str, project_name: str) -> str | None:
    """Project overview tab (matches ynput/ayon-frontend ProjectPage nav)."""
    base = (ayon_base_url or "").rstrip("/")
    pn = str(project_name or "").strip()
    if not base or not pn:
        return None
    return f"{base}/projects/{_encode_project_segment(pn)}/overview"


def _ayon_details_panel_query(
    project_name: str,
    *,
    entity_type: str,
    entity_id: str,
) -> str:
    """Query string matching ynput/ayon-frontend Details panel URL sync.

    See ``useDetailsPanelURLSync`` / ``DetailsPanelContext`` (``project``,
    ``type``, ``id``).
    """
    pn = str(project_name or "").strip()
    eid = str(entity_id or "").strip()
    et = str(entity_type or "").strip()
    return urllib.parse.urlencode(
        (("project", pn), ("type", et), ("id", eid)),
        doseq=False,
    )


def ayon_browser_url_task_context(
    ayon_base_url: str,
    project_name: str,
    *,
    ayon_task_id: str | None = None,
    ayon_folder_id: str | None = None,
) -> str | None:
    """Task progress deep link aligned with ynput/ayon-frontend Details panel.

    Uses ``/projects/…/tasks`` plus ``project``, ``type``, and ``id`` query
    parameters (same as ``useDetailsPanelURLSync``) so the SPA resolves the
    entity and runs ``onUriOpen`` selection on Task progress.
    """
    base = (ayon_base_url or "").rstrip("/")
    pn = str(project_name or "").strip()
    if not base or not pn:
        return None
    enc = _encode_project_segment(pn)
    url = f"{base}/projects/{enc}/tasks"
    tid = str(ayon_task_id or "").strip()
    fid = str(ayon_folder_id or "").strip()
    if tid:
        url += "?" + _ayon_details_panel_query(
            pn, entity_type="task", entity_id=tid
        )
    elif fid:
        url += "?" + _ayon_details_panel_query(
            pn, entity_type="folder", entity_id=fid
        )
    return url


def ayon_browser_url_workfiles(
    ayon_base_url: str,
    project_name: str,
    *,
    ayon_task_id: str | None = None,
    ayon_folder_id: str | None = None,
) -> str | None:
    """Workfiles module path with the same ``project`` / ``type`` / ``id`` contract."""
    base = (ayon_base_url or "").rstrip("/")
    pn = str(project_name or "").strip()
    if not base or not pn:
        return None
    enc = _encode_project_segment(pn)
    url = f"{base}/projects/{enc}/workfiles"
    tid = str(ayon_task_id or "").strip()
    fid = str(ayon_folder_id or "").strip()
    if tid:
        url += "?" + _ayon_details_panel_query(
            pn, entity_type="task", entity_id=tid
        )
    elif fid:
        url += "?" + _ayon_details_panel_query(
            pn, entity_type="folder", entity_id=fid
        )
    return url


# --- Kitsu publish comment table → Products tab (``uri`` query, ynput/ayon-frontend) ---


def parse_kitsu_publish_comment_table_version(comment_text: str) -> int | None:
    """Parse ``version`` from the GFM table produced by IntegrateKitsuNote templates.

    Matches rows like ``| version | `5` |`` (backticks optional). Stops at the first
    valid positive integer. Optional ``task`` / ``uniqueSprites`` rows may be absent.
    """
    if not isinstance(comment_text, str) or not comment_text.strip():
        return None
    for line in comment_text.replace("\r\n", "\n").split("\n"):
        raw = line.strip()
        m = re.match(
            r"^\|\s*version\s*\|\s*(?:`([^`]+)`|([^|]+?))\s*\|\s*$",
            raw,
            flags=re.IGNORECASE,
        )
        if not m:
            continue
        cell = (m.group(1) or m.group(2) or "").strip()
        if not cell:
            continue
        try:
            n = int(cell)
        except ValueError:
            continue
        if n > 0:
            return n
    return None


def ayon_entity_uri_product_version(
    project_name: str,
    folder_path: str,
    *,
    product_name: str,
    version: int | str,
) -> str:
    """``ayon+entity://…`` URI aligned with ``BrowserPage/Products/Products.jsx``."""
    pn = str(project_name or "").strip()
    fp = (folder_path or "").strip().lstrip("/")
    prod = str(product_name or "").strip()
    ver = str(version).strip()
    return f"ayon+entity://{pn}/{fp}?product={prod}&version={ver}"


def ayon_browser_url_products_with_uri(
    ayon_base_url: str,
    project_name: str,
    entity_uri: str,
) -> str | None:
    """Products tab URL carrying ``uri=`` (encoded ``ayon+entity://`` payload)."""
    base = (ayon_base_url or "").rstrip("/")
    pn = str(project_name or "").strip()
    eu = str(entity_uri or "").strip()
    if not base or not pn or not eu:
        return None
    enc = _encode_project_segment(pn)
    q = urllib.parse.urlencode({"uri": eu})
    return f"{base}/projects/{enc}/products?{q}"
