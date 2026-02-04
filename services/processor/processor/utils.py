"""utils shared between fullsync.py and update_from_kitsu.py"""

import re
import threading
from typing import Any, Dict

import ayon_api
import gazu

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
    ayon_users = {
        user["attrib"]["email"]: user["name"] for user in ayon_api.get_users()
    }
    task_emails = {user["email"] for user in task["persons"]}
    task["assignees"] = []
    task["assignees"].extend(
        ayon_users[email] for email in task_emails if email in ayon_users
    )

    return task


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
            return "" if key not in data else str(data[key])

        pattern = r"\{([^}]*)\}"
        return re.sub(pattern, replace_missing_key, template)

    # Fallback to tab-delimited format (preserves line breaks in Kitsu)
    version = data.get("version", "")
    family = data.get("family", "")
    name = data.get("name", "")
    unique_sprites = data.get("uniqueSprites", "")

    result = f"version\t{version}\nfamily\t{family}\nname\t{name}"
    if unique_sprites:
        result += f"\nuniqueSprites\t{unique_sprites}"
    return result
