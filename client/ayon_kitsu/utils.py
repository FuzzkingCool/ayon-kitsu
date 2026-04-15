import re
from typing import Any, Dict, Mapping, MutableMapping, Optional

import gazu


def is_kitsu_task_row(obj: Any) -> bool:
    """True if ``obj`` looks like a Kitsu *task* dict (not a shot/asset entity).

    Kitsu task payloads include ``task_type_id``; entity dicts from
    ``gazu.entity.get_entity`` typically do not.
    """
    return isinstance(obj, dict) and obj.get("task_type_id") is not None


def kitsu_task_type_lookup_name(task_entity: Mapping[str, Any]) -> str:
    """Name to pass to ``gazu.task.get_task_type_by_name`` when ``kitsuId`` is absent.

    Checklist-style AYON tasks may use a slug ``name`` that is not a Kitsu task type.
    When ``name`` and ``taskType`` name differ (case-insensitive), use the type name
    so the canonical Kitsu task row for that pipeline type is resolved.
    """
    task_name = (task_entity.get("name") or "").strip()
    tt = task_entity.get("taskType")
    if isinstance(tt, dict):
        task_type_name = (tt.get("name") or "").strip()
    elif isinstance(tt, str):
        task_type_name = tt.strip()
    else:
        task_type_name = ""
    if task_type_name and task_name.lower() != task_type_name.lower():
        return task_type_name
    return task_name


def resolve_canonical_kitsu_task(
    task_entity: Mapping[str, Any],
    kitsu_entity: Mapping[str, Any],
    *,
    kitsu_entities_by_id: Optional[MutableMapping[str, Any]] = None,
    log: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve the canonical Kitsu task row for an AYON task (publish target on Kitsu).

    Checklist child tasks in AYON use a custom ``name`` but share the pipeline
    ``taskType``; Kitsu has a single task row per (entity, task type). Resolution
    order:

    1. ``task_entity.data.kitsuId`` — use only if it resolves to a task row (never
       reuse a cached shot/asset dict keyed by the same id).
    2. ``task_entity.data.kitsuParentKitsuTaskId`` — explicit parent Kitsu task.
    3. Lookup by ``kitsu_task_type_lookup_name(task_entity)`` on ``kitsu_entity``.

    Returns ``None`` if the task row cannot be resolved.
    """
    cache = kitsu_entities_by_id
    task_data = task_entity.get("data") if isinstance(task_entity, dict) else {}
    if not isinstance(task_data, dict):
        task_data = {}
    task_name = (task_entity.get("name") or "").strip()

    def _log_info(msg: str, *args: Any) -> None:
        if log is not None and hasattr(log, "info"):
            log.info(msg, *args)

    def _task_from_id(task_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not task_id:
            return None
        cached: Any = None
        if cache is not None:
            cached = cache.get(task_id)
        if cached is not None and is_kitsu_task_row(cached):
            return cached
        if cached is not None and not is_kitsu_task_row(cached):
            # Poisoned cache (e.g. folder entity id == task id) — fetch task by id.
            pass
        row = gazu.task.get_task(task_id)
        return row if isinstance(row, dict) and is_kitsu_task_row(row) else None

    kitsu_task_id = task_data.get("kitsuId")
    kitsu_task: Optional[Dict[str, Any]] = None

    if kitsu_task_id:
        kitsu_task = _task_from_id(kitsu_task_id)
        if kitsu_task is None and log is not None:
            _log_info(
                "AYON task %s kitsuId %r did not resolve to a Kitsu task row; "
                "trying parent id / type lookup.",
                task_entity.get("id"),
                kitsu_task_id,
            )

    if kitsu_task is None:
        parent_id = task_data.get("kitsuParentKitsuTaskId")
        kitsu_task = _task_from_id(parent_id)
        if kitsu_task is not None:
            _log_info(
                "Resolved Kitsu task via kitsuParentKitsuTaskId for AYON task %s",
                task_entity.get("id"),
            )

    if kitsu_task is None:
        lookup_name = kitsu_task_type_lookup_name(task_entity)
        if not lookup_name:
            return None
        if lookup_name != task_name:
            _log_info(
                "Kitsu task lookup redirect: AYON task id=%s name=%r != taskType; "
                "using type name %r for get_task_type_by_name on Kitsu entity %s",
                task_entity.get("id"),
                task_name,
                lookup_name,
                kitsu_entity.get("id"),
            )
        kitsu_task_type = gazu.task.get_task_type_by_name(lookup_name)
        if not kitsu_task_type:
            return None
        kitsu_task = gazu.task.get_task_by_name(kitsu_entity, kitsu_task_type)
        if kitsu_task and lookup_name != task_name:
            _log_info(
                "Kitsu task resolved for redirect: kitsu_task_id=%s kitsu name=%r",
                kitsu_task.get("id"),
                kitsu_task.get("name"),
            )

    if not kitsu_task or not is_kitsu_task_row(kitsu_task):
        return None
    return kitsu_task


def format_kitsu_task_display(value: Any) -> str:
    """Format task for Kitsu comment placeholders and tab lines.

    Anatomy-style dicts ``{"name", "type", "short"}`` become ``name ( type )``.
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

