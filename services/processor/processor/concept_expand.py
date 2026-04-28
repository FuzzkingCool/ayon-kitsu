"""Concept row expansion for ``per_linked_entity`` sync (no ``ayon_api`` dependency)."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import gazu
from nxtools import logging

from .concept_naming import apply_concept_title_for_ayon_push

_CONCEPT_ENTITY_MODEL_PER_LINKED = "per_linked_entity"
_MAX_KITSU_SOURCE_CONCEPT_IDS = 50

# Processor JSON may use camelCase (mirrors studio REST); normalize before reads.
_CONCEPT_SYNC_CAMEL_TO_SNAKE: Dict[str, str] = {
    "conceptEntityModel": "concept_entity_model",
    "unlinkedConceptsAnchor": "unlinked_concepts_anchor",
    "unlinkedConceptsFolderLabel": "unlinked_concepts_folder_label",
    "unlinkedConceptsHubKitsuId": "unlinked_concepts_hub_kitsu_id",
    "unlinkedConceptsProjectFolderLabel": "unlinked_concepts_project_folder_label",
    "unlinkedConceptsProjectKitsuId": "unlinked_concepts_project_kitsu_id",
}

DEFAULT_UNLINKED_PROJECT_KITSU_ID = "kitsu:concepts:unlinked_project"

# Processor / Docker: overrides ``sync_settings.concept_sync.concept_entity_model`` when set.
_ENV_CONCEPT_ENTITY_MODEL = "KITSU_PROCESSOR_CONCEPT_ENTITY_MODEL"


def normalize_concept_sync_dict(
    raw: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Copy ``concept_sync``, map camelCase keys, apply env override, drop blank model.

    If ``raw`` is ``None`` or empty ``{}``, still returns a dict when
    ``KITSU_PROCESSOR_CONCEPT_ENTITY_MODEL`` is set (processor-only override without
    nesting keys in service JSON).
    """
    env_cem = os.environ.get(_ENV_CONCEPT_ENTITY_MODEL, "").strip()

    if raw is not None and not isinstance(raw, dict):
        return None

    if not raw:
        if env_cem:
            return {"concept_entity_model": env_cem}
        return None

    out: Dict[str, Any] = dict(raw)
    for camel, snake in _CONCEPT_SYNC_CAMEL_TO_SNAKE.items():
        if camel in out and snake not in out:
            out[snake] = out[camel]

    if env_cem:
        out["concept_entity_model"] = env_cem

    cem = str(out.get("concept_entity_model") or "").strip()
    if cem:
        out["concept_entity_model"] = cem
    else:
        out.pop("concept_entity_model", None)

    return out if out else None


def unlinked_concepts_anchor_is_project(
    concept_sync: Optional[Dict[str, Any]],
) -> bool:
    """True when unlinked Kitsu concepts should sync under a Project folder, not Concept hub."""
    if not concept_sync:
        return False
    v = str(concept_sync.get("unlinked_concepts_anchor") or "").strip().lower()
    return v == "project"


def concept_entity_model_is_per_linked_dict(
    concept_sync: Optional[Dict[str, Any]],
) -> bool:
    """True when ``sync_settings.concept_sync.concept_entity_model`` is per-linked."""
    if not concept_sync:
        return False
    return (
        str(concept_sync.get("concept_entity_model") or "").strip()
        == _CONCEPT_ENTITY_MODEL_PER_LINKED
    )


def per_linked_effective_linked_id_for_relink(
    concept_entity: Dict[str, Any],
    concept_sync: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Folder key for per-linked relink: link id, including single-link legacy payloads."""
    if concept_entity.get("type") != "Concept":
        return None
    if concept_entity.get("__conceptSyncModel") == "per_linked_entity":
        lid = str(concept_entity.get("id") or "").strip()
        return lid or None
    if not concept_entity_model_is_per_linked_dict(concept_sync):
        return None
    links = normalize_concept_link_ids(concept_entity.get("entity_concept_links"))
    if len(links) == 1:
        return links[0]
    return None


def should_attempt_per_linked_concept_relink(
    ent: Dict[str, Any],
    concept_sync: Optional[Dict[str, Any]],
) -> bool:
    return per_linked_effective_linked_id_for_relink(ent, concept_sync) is not None


def normalize_concept_link_ids(links: Any) -> List[str]:
    if not links or not isinstance(links, (list, tuple)):
        return []
    out: List[str] = []
    for lid in links:
        eid = str(lid).strip() if lid is not None else ""
        if eid and eid not in out:
            out.append(eid)
    return out


def _linked_entity_dict_for_concept_push(entity_id: str) -> Dict[str, Any] | None:
    """Callers must have set the Kitsu API host (``set_kitsu_host`` / fullsync)."""
    try:
        ent = gazu.entity.get_entity(entity_id)
    except Exception:
        logging.debug(
            f"[concept_expand] gazu.entity.get_entity failed for {entity_id!r}",
            exc_info=True,
        )
        return None
    return ent if isinstance(ent, dict) else None


def expand_concept_entities_for_push(
    concepts: List[Dict[str, Any]],
    concept_sync: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Expand/dedupe concept rows for POST /push when ``concept_entity_model`` is per-linked.

    Linked concepts become **one** payload per ``linked_entity_id`` (Kitsu asset /
    entity uuid). Parent ids from different concept rows that point at the same
    link are merged (prefer a non-empty ``parent_id``) so fullsync never sends
    duplicate creates that collide on ``(parent_id, folder.name)``.
    Unlinked concepts (no ``entity_concept_links``) keep one row per concept id;
    those with no Kitsu ``parent_id`` nest under a synthetic hub folder.
    """
    if not concepts:
        return concepts
    if not concept_entity_model_is_per_linked_dict(concept_sync):
        return list(concepts)

    hub_label = (concept_sync or {}).get("unlinked_concepts_folder_label") or (
        "Unlinked concepts"
    )
    hub_id = (concept_sync or {}).get("unlinked_concepts_hub_kitsu_id") or (
        "kitsu:concepts:unlinked_hub"
    )

    project_id = ""
    for c in concepts:
        if isinstance(c, dict) and c.get("project_id"):
            project_id = str(c["project_id"])
            break

    dedupe_rows: Dict[str, Dict[str, Any]] = {}
    unlinked_out: List[Dict[str, Any]] = []
    need_hub = False
    need_project_anchor = False

    def _parent_key(row: Dict[str, Any]) -> str:
        p = row.get("parent_id")
        if p is None:
            return ""
        return str(p).strip()

    for c in concepts:
        if not isinstance(c, dict):
            continue
        base = dict(c)
        links = normalize_concept_link_ids(base.get("entity_concept_links"))

        if not links:
            if unlinked_concepts_anchor_is_project(concept_sync):
                need_project_anchor = True
                continue
            row = dict(base)
            row.pop("__conceptSyncModel", None)
            if not _parent_key(row):
                row["parent_id"] = hub_id
                need_hub = True
            row["__conceptSyncModel"] = "per_concept_unlinked"
            row.setdefault("type", "Concept")
            unlinked_out.append(apply_concept_title_for_ayon_push(row))
            continue

        for lid in links:
            cid = str(base.get("id") or "").strip()
            ent = _linked_entity_dict_for_concept_push(lid)
            if lid not in dedupe_rows:
                row = dict(base)
                row["id"] = lid
                row["__conceptSyncModel"] = "per_linked_entity"
                row["kitsuSourceConceptIds"] = [cid] if cid else []
                if ent:
                    nm = (ent.get("name") or "").strip()
                    cd = (ent.get("code") or "").strip()
                    if nm:
                        row["name"] = nm
                    if cd:
                        row["code"] = cd
                    et = ent.get("entity_type_id")
                    if et:
                        row["entity_type_id"] = et
                    row["linked_entity_names"] = [nm] if nm else []
                row.pop("entity_concept_links", None)
                row.setdefault("type", "Concept")
                dedupe_rows[lid] = apply_concept_title_for_ayon_push(row)
            else:
                agg = dedupe_rows[lid]
                ids = list(agg.get("kitsuSourceConceptIds") or [])
                if cid and cid not in ids:
                    ids.append(cid)
                    if len(ids) > _MAX_KITSU_SOURCE_CONCEPT_IDS:
                        ids = ids[:_MAX_KITSU_SOURCE_CONCEPT_IDS]
                agg["kitsuSourceConceptIds"] = ids
                pf = base.get("preview_file_id")
                if pf and not agg.get("preview_file_id"):
                    agg["preview_file_id"] = pf
                bp = _parent_key(base)
                ap = _parent_key(agg)
                if bp and ap and bp != ap:
                    logging.warning(
                        f"[concept_expand] conflicting Kitsu parent_id for linked entity "
                        f"{lid[:8]} ({ap[:12]!r} vs {bp[:12]!r}); keeping first parent"
                    )
                elif bp and not ap:
                    agg["parent_id"] = base.get("parent_id")

    hub_entity: Dict[str, Any] | None = None
    if need_hub:
        hub_entity = apply_concept_title_for_ayon_push(
            {
                "type": "Concept",
                "id": hub_id,
                "name": hub_label,
                "code": hub_label,
                "parent_id": None,
                "project_id": project_id,
                "__conceptSyncModel": "unlinked_hub",
            }
        )

    project_anchor: Dict[str, Any] | None = None
    if need_project_anchor:
        plabel = (concept_sync or {}).get("unlinked_concepts_project_folder_label")
        plabel = (str(plabel).strip() if plabel is not None else "") or "Project"
        pkid = (concept_sync or {}).get("unlinked_concepts_project_kitsu_id")
        pkid = (str(pkid).strip() if pkid is not None else "") or DEFAULT_UNLINKED_PROJECT_KITSU_ID
        project_anchor = {
            "type": "Project",
            "id": pkid,
            "name": plabel,
            "code": plabel,
            "parent_id": None,
            "project_id": project_id,
            "__conceptSyncModel": "unlinked_project_anchor",
        }

    linked_sorted = sorted(
        dedupe_rows.values(),
        key=lambda r: (_parent_key(r), str(r.get("id") or "")),
    )

    out: List[Dict[str, Any]] = []
    if hub_entity is not None:
        out.append(hub_entity)
    if project_anchor is not None:
        out.append(project_anchor)
    out.extend(linked_sorted)
    out.extend(unlinked_out)
    return out


def expand_single_concept_entity_for_push(
    entity: Dict[str, Any],
    concept_sync: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Same expansion rules as fullsync, for one concept (incremental /push)."""
    return expand_concept_entities_for_push([entity], concept_sync=concept_sync)
