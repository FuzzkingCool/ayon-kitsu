"""utils shared between fullsync.py and update_from_kitsu.py"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Optional, cast

import ayon_api
import gazu
from nxtools import logging

from .concept_naming import apply_concept_title_for_ayon_push

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


def _concept_sync_prefer_linked_names(
    concept_sync: Optional[Dict[str, Any]],
) -> bool:
    """Studio ``sync_settings.concept_sync.prefer_linked_asset_names`` (default True)."""
    if not concept_sync:
        return True
    return bool(concept_sync.get("prefer_linked_asset_names", True))


def _fetch_linked_entity_names_for_concept(concept: Dict[str, Any]) -> List[str]:
    """Resolve ``entity_concept_links`` to entity ``name`` strings (Kitsu ConceptCard order)."""
    links = concept.get("entity_concept_links")
    if not links or not isinstance(links, (list, tuple)):
        return []
    _ensure_gazu_host()
    out: List[str] = []
    for lid in links:
        eid = str(lid).strip() if lid is not None else ""
        if not eid:
            continue
        try:
            ent = gazu.entity.get_entity(eid)
        except Exception:
            logging.debug(
                "[kitsu] get_entity failed for concept link entity_id=%r",
                eid,
                exc_info=True,
            )
            continue
        if not isinstance(ent, dict):
            continue
        nm = (ent.get("name") or "").strip()
        if nm:
            out.append(nm)
    return out


def _enrich_concept_with_linked_names(
    concept: Dict[str, Any],
    *,
    concept_sync: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not _concept_sync_prefer_linked_names(concept_sync):
        return concept
    names = _fetch_linked_entity_names_for_concept(concept)
    if not names:
        return concept
    merged = dict(concept)
    merged["linked_entity_names"] = names
    return merged


def _merge_concept_list_row_with_canonical(
    row: Dict[str, Any],
    concept_id: str,
    concept_sync: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Overlay ``gazu.concept.get_concept`` on a ``GET /data/concepts`` row.

    List responses can lag or omit fields; canonical fetch drives renames,
    descriptions, and ``preview_file_id`` for sync payloads.
    """
    _ensure_gazu_host()
    base = dict(row)
    base.setdefault("type", "Concept")
    try:
        canonical = gazu.concept.get_concept(concept_id)
    except Exception:
        logging.debug(
            "[kitsu] get_concept failed during merge concept_id=%r",
            concept_id,
            exc_info=True,
        )
        return apply_concept_title_for_ayon_push(
            _enrich_concept_with_linked_names(base, concept_sync=concept_sync)
        )
    if not isinstance(canonical, dict):
        return apply_concept_title_for_ayon_push(
            _enrich_concept_with_linked_names(base, concept_sync=concept_sync)
        )
    # Do not let canonical nulls wipe list-row fields (Zou often omits keys but
    # still sends JSON nulls for unused columns, which would drop a good code).
    merged = dict(base)
    for key, value in canonical.items():
        if value is not None:
            merged[key] = value
    merged.setdefault("type", "Concept")
    merged = _enrich_concept_with_linked_names(merged, concept_sync=concept_sync)
    return apply_concept_title_for_ayon_push(merged)


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
    concept_sync: Optional[Dict[str, Any]] = None,
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
            return _merge_concept_list_row_with_canonical(
                hit, cid, concept_sync=concept_sync
            )

    if parent_id is not None and str(parent_id) != "":
        lst2 = fetch_concepts_list_via_data_endpoint(pid, str(parent_id))
        if lst2 is not None:
            hit = _pick(lst2)
            if hit is not None:
                return _merge_concept_list_row_with_canonical(
                    hit, cid, concept_sync=concept_sync
                )

    raw = gazu.concept.get_concept(concept_id)
    if raw is None or not isinstance(raw, dict):
        return raw
    enriched = _enrich_concept_with_linked_names(
        dict(raw), concept_sync=concept_sync
    )
    return apply_concept_title_for_ayon_push(enriched)


def all_concepts_for_project_official_list(
    project: Any,
    concept_sync: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """All concepts for fullsync: prefer ``GET /data/concepts?project_id=``.

    Falls back to ``gazu.concept.all_concepts_for_project`` if the list route
    fails or returns no rows.

    Each list row is merged with ``gazu.concept.get_concept`` so pushes carry
    fresh names and preview ids (list-only payloads can be stale).

    **Repair:** after deploying this merge behavior, run a project **fullsync**
    once so AYON Concept folder labels catch up with Kitsu via normal
    ``sync_folder`` updates; no separate DB migration is required for renames.
    """
    _ensure_gazu_host()
    if isinstance(project, dict):
        pid = str(project.get("id", ""))
    else:
        pid = str(project)
    if not pid:
        return _normalize_concept_rows_for_ayon(
            gazu.concept.all_concepts_for_project(project),
            concept_sync=concept_sync,
        )
    rows = fetch_concepts_list_via_data_endpoint(pid, None)
    if rows is None:
        return _normalize_concept_rows_for_ayon(
            gazu.concept.all_concepts_for_project(project),
            concept_sync=concept_sync,
        )
    merged: List[Dict[str, Any]] = []
    for row in rows:
        cid = row.get("id")
        if cid:
            merged.append(
                _merge_concept_list_row_with_canonical(
                    row, str(cid), concept_sync=concept_sync
                )
            )
        else:
            r = dict(row)
            r.setdefault("type", "Concept")
            merged.append(
                apply_concept_title_for_ayon_push(
                    _enrich_concept_with_linked_names(r, concept_sync=concept_sync)
                )
            )
    return merged


def _normalize_concept_rows_for_ayon(
    rows: List[Dict[str, Any]] | None,
    concept_sync: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    if not rows:
        return []
    out: List[Dict[str, Any]] = []
    for c in rows:
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        if cid:
            out.append(
                _merge_concept_list_row_with_canonical(
                    dict(c), str(cid), concept_sync=concept_sync
                )
            )
        else:
            r = dict(c)
            r.setdefault("type", "Concept")
            out.append(
                apply_concept_title_for_ayon_push(
                    _enrich_concept_with_linked_names(r, concept_sync=concept_sync)
                )
            )
    return out


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


from .concept_expand import (  # noqa: E402 — re-export after package init
    DEFAULT_UNLINKED_PROJECT_KITSU_ID,
    concept_entity_model_is_per_linked_dict,
    expand_concept_entities_for_push,
    expand_single_concept_entity_for_push,
    normalize_concept_sync_dict,
    normalize_concept_link_ids,
    unlinked_concepts_anchor_is_project,
)
