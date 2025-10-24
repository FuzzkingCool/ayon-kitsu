import re
from typing import Any, Dict, Optional

import gazu


def render_kitsu_comment(template_cfg: Dict[str, Any], data: Dict[str, Any]) -> str:
    """Render a Kitsu comment using the same logic as IntegrateKitsuNote.

    Expects a template configuration of the form:
        {"enabled": bool, "comment_template": str}

    Replaces brace-delimited keys (e.g. {version}) with values from ``data``.
    If the template is disabled or missing, falls back to a simple tab-delimited
    version/family/name format.
    """
    enabled = False
    template = None
    if isinstance(template_cfg, dict):
        enabled = bool(template_cfg.get("enabled"))
        template = template_cfg.get("comment_template")

    if enabled and template:
        # Replace unknown keys with empty string, similar to IntegrateKitsuNote
        def replace_missing_key(match: re.Match[str]) -> str:
            key = match.group(1)
            return "" if key not in data else str(data[key])

        pattern = r"\{([^}]*)\}"
        return re.sub(pattern, replace_missing_key, template)

    # Fallback to tab-delimited format (preserves line breaks in Kitsu)
    version = data.get("version", "")
    family = data.get("family", "")
    name = data.get("name", "")
    return f"version\t{version}\nfamily\t{family}\nname\t{name}"


def resolve_feedback_status(kitsu_task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Resolve the desired Kitsu task status for feedback comments.

    Preference order:
      1) Short name 'Feedback'
      2) Name 'Feedback Needed'
      3) Current task status from the task payload
    """
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

    return kitsu_task.get("task_status") if isinstance(kitsu_task, dict) else None

