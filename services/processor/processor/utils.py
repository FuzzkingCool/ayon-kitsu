"""utils shared between fullsync.py and update_from_kitsu.py"""

import re
import threading
from typing import Any, Dict, List, Optional, cast

import ayon_api
import gazu
from nxtools import logging

# Thread-local Kitsu API URL so gazu uses the correct host in every thread
# (gazu default is http://gazu.change.serverhost/api; event handlers run in listener thread)
_kitsu_host_local = threading.local()


def set_kitsu_host(url: str | None) -> None:
    """Set Kitsu API URL for the current thread; gazu requests will use it."""
    _kitsu_host_local.url = url


def get_kitsu_host() -> str | None:
    """Get Kitsu API URL for the current thread, or None."""
    return getattr(_kitsu_host_local, "url", None)


def _ensure_gazu_host() -> None:
    """Set gazu client host from thread-local URL so the next gazu request uses it."""
    url = get_kitsu_host()
    if url:
        gazu.set_host(url)


def resolve_feedback_status(kitsu_task: dict[str, str] | None) -> dict | None:
    """Resolve the desired Kitsu task status for feedback comments.

    Preference order:
      1) Short name 'Feedback'
      2) Name 'Feedback Needed'
      3) Current task status from the task payload
    """
    _ensure_gazu_host()
    try:
        status = gazu.task.get_task_status_by_short_name("FEEDBACK")
        if status:
            return status
    except Exception:
        pass

    try:
        status = gazu.task.get_task_status_by_name("Feedback Needed")
        if status:
            return status
    except Exception:
        pass

    return (
        kitsu_task.get("task_status") if isinstance(kitsu_task, dict) else None
    )


def get_asset_types(kitsu_project_id: str) -> dict[str, str]:
    _ensure_gazu_host()
    raw_asset_types = gazu.asset.all_asset_types_for_project(kitsu_project_id)
    kitsu_asset_types = {}
    for asset_type in raw_asset_types:
        kitsu_asset_types[asset_type["id"]] = asset_type["name"]
    return kitsu_asset_types


def get_task_types(kitsu_project_id: str) -> dict[str, str]:
    _ensure_gazu_host()
    raw_task_types = gazu.task.all_task_types_for_project(kitsu_project_id)
    kitsu_task_types = {}
    for task_type in raw_task_types:
        kitsu_task_types[task_type["id"]] = task_type["name"]
    return kitsu_task_types


def get_statuses() -> dict[str, str]:
    _ensure_gazu_host()
    raw_statuses = gazu.task.all_task_statuses()
    kitsu_statuses = {}
    for status in raw_statuses:
        kitsu_statuses[status["id"]] = status["name"]
    return kitsu_statuses


def preprocess_asset(
    kitsu_project_id: str,
    asset: dict[str, str],
    asset_types: dict[str, str] = {},
) -> dict[str, str]:
    if not asset_types:
        asset_types = get_asset_types(kitsu_project_id)

    if "entity_type_id" in asset and asset["entity_type_id"] in asset_types:
        asset["asset_type_name"] = asset_types[asset["entity_type_id"]]
    return asset


def preprocess_task(
    kitsu_project_id: str,
    task: dict[str, str | list[str]],
    task_types: dict[str, str | list[str]] = {},
    statuses: dict[str, str] = {},
    ayon_users_by_email: dict[str, str] | None = None,
) -> dict[str, str | list[str]]:
    if not task_types:
        task_types = get_task_types(kitsu_project_id)

    if not statuses:
        statuses = get_statuses()

    if "task_type_id" in task and task["task_type_id"] in task_types:
        task["task_type_name"] = task_types[task["task_type_id"]]

    if "task_status_id" in task and task["task_status_id"] in statuses:
        task["task_status_name"] = statuses[task["task_status_id"]]

    if "name" in task and "task_type_name" in task and task["name"] == "main":
        task["name"] = task["task_type_name"].lower()

    # Match the assigned ayon user with the assigned kitsu email
    if ayon_users_by_email is None:
        ayon_users_by_email = {
            user["attrib"]["email"]: user["name"] for user in ayon_api.get_users()
        }
    task_emails = {user["email"] for user in task["persons"]}
    task["assignees"] = []
    task["assignees"].extend(
        ayon_users_by_email[email] for email in task_emails if email in ayon_users_by_email
    )

    return task


def _concepts_rows_from_get_response(body: Any) -> List[Dict[str, Any]]:
    """Normalize GET /data/concepts JSON body to a list of concept dicts."""
    if isinstance(body, list):
        return [cast(Dict[str, Any], x) for x in body if isinstance(x, dict)]
    if isinstance(body, dict):
        inner = body.get("data")
        if isinstance(inner, list):
            return [cast(Dict[str, Any], x) for x in inner if isinstance(x, dict)]
        if body.get("id"):
            return [cast(Dict[str, Any], body)]
    return []


def fetch_concepts_list_via_data_endpoint(
    project_id: str,
    parent_id: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """GET /data/concepts?project_id=…&parent_id=… (official Zou list API).

    Returns ``None`` on request failure; may return an empty list when the API
    responds successfully but has no rows.
    """
    _ensure_gazu_host()
    params: Dict[str, str] = {"project_id": str(project_id)}
    if parent_id is not None and str(parent_id) != "":
        params["parent_id"] = str(parent_id)
    try:
        body = gazu.client.get("data/concepts", params=params)
        return _concepts_rows_from_get_response(body)
    except Exception:
        logging.debug(
            "[kitsu] GET data/concepts failed project_id=%r parent_id=%r",
            project_id,
            parent_id,
            exc_info=True,
        )
        return None


def load_concept_entity_for_sync(
    project_id: str,
    concept_id: str,
    parent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Load one concept for AYON push, preferring official ``GET /data/concepts``.

    Tries project-wide list first, then filtered by ``parent_id`` when given,
    then ``gazu.concept.get_concept`` as fallback.
    """
    _ensure_gazu_host()
    cid = str(concept_id)
    pid = str(project_id)

    def _pick(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        for row in rows:
            if str(row.get("id")) == cid:
                merged = dict(row)
                merged.setdefault("type", "Concept")
                return merged
        return None

    lst = fetch_concepts_list_via_data_endpoint(pid, None)
    if lst is not None:
        hit = _pick(lst)
        if hit is not None:
            return hit

    if parent_id is not None and str(parent_id) != "":
        lst2 = fetch_concepts_list_via_data_endpoint(pid, str(parent_id))
        if lst2 is not None:
            hit = _pick(lst2)
            if hit is not None:
                return hit

    return gazu.concept.get_concept(concept_id)


def all_concepts_for_project_official_list(project: Any) -> List[Dict[str, Any]]:
    """All concepts for fullsync: prefer ``GET /data/concepts?project_id=``.

    Falls back to ``gazu.concept.all_concepts_for_project`` if the list route
    fails or returns no rows.
    """
    _ensure_gazu_host()
    if isinstance(project, dict):
        pid = str(project.get("id", ""))
    else:
        pid = str(project)
    if not pid:
        return gazu.concept.all_concepts_for_project(project)
    rows = fetch_concepts_list_via_data_endpoint(pid, None)
    if rows is None:
        return gazu.concept.all_concepts_for_project(project)
    for c in rows:
        c.setdefault("type", "Concept")
    return rows


def format_kitsu_task_display(value: Any) -> str:
    """Format task for Kitsu comment placeholders and tab lines.

    Anatomy-style dicts ``{"name", "type", "short"}`` become ``name ( type )``.

    Keep in sync with client/ayon_kitsu/utils.py:format_kitsu_task_display.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        name = (value.get("name") or "").strip()
        typ = (value.get("type") or "").strip()
        if name and typ:
            return f"{name} ( {typ} )"
        if name:
            return name
        if typ:
            return typ
    return str(value)


def render_kitsu_comment(
    template_cfg: Dict[str, Any], data: Dict[str, Any]
) -> str:
    """Render a Kitsu comment using template configuration.

    Expects a template configuration of the form:
        {"enabled": bool, "comment_template": str}

    Replaces brace-delimited keys (e.g. {version}) with values from ``data``.
    If the template is disabled or missing, falls back to a simple tab-delimited
    version/family/name format.

    This is a duplicate of client/ayon_kitsu/utils.py:render_kitsu_comment
    to avoid importing client modules in the processor service.
    """
    enabled = False
    template = None
    if isinstance(template_cfg, dict):
        enabled = bool(template_cfg.get("enabled"))
        template = template_cfg.get("comment_template")

    if enabled and template:
        # Replace unknown keys with empty string
        def replace_missing_key(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in data:
                return ""
            if key in ("task", "task_name"):
                return format_kitsu_task_display(data[key])
            return str(data[key])

        pattern = r"\{([^}]*)\}"
        result = re.sub(pattern, replace_missing_key, template)
        # Omit uniqueSprites line when value is 0 or empty (tab or table template format)
        if str(data.get("uniqueSprites", "")).strip() in ("", "0"):
            result = re.sub(r"\n[^\n]*uniqueSprites[^\n]*", "", result)
        return result

    # Fallback to tab-delimited format (preserves line breaks in Kitsu)
    version = data.get("version", "")
    family = data.get("family", "")
    name = data.get("name", "")
    unique_sprites = data.get("uniqueSprites", "")

    result = f"version\t{version}\nfamily\t{family}\nname\t{name}"
    task_val = format_kitsu_task_display(data.get("task_name"))
    if task_val:
        result += f"\ntask_name\t{task_val}"
    # Omit uniqueSprites when 0 (counting was skipped) or empty
    if unique_sprites not in (None, "", 0, "0"):
        result += f"\nuniqueSprites\t{unique_sprites}"
    return result
