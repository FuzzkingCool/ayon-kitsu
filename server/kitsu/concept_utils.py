"""Pure helpers for Kitsu Concept → AYON display naming and VizDev surrogate ids."""

from __future__ import annotations

import re
from typing import Any

from nxtools import slugify

# Kitsu often names concepts ``<stem>.<ext>-<uuid>`` (see tests mock_data).
_KITSU_CONCEPT_NAME_UUID_RE = re.compile(
    r"^(.+)(\.[A-Za-z0-9]{1,15})-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
# Auto-import / file-upload style ``123456789-descriptive_name``.
_KITSU_CONCEPT_LEADING_NUMERIC_PREFIX_RE = re.compile(r"^(\d{6,})-(.+)$")


def concept_vizdev_surrogate_kitsu_id(concept_id: str) -> str:
    """Deterministic id for the AYON-only VizDev task; must not be a real Kitsu UUID."""
    return f"kitsu:concept:{concept_id}:vizdev"


def concept_vizdev_surrogate_for_linked_entity(linked_entity_id: str) -> str:
    """VizDev surrogate when Concept folders use ``data.kitsuId`` = linked Kitsu entity id."""
    return f"kitsu:link:{linked_entity_id}:vizdev"


def concept_vizdev_surrogate_unlinked_pool() -> str:
    """VizDev task id for all Kitsu concepts with no links under the Project anchor."""
    return "kitsu:concepts:unlinked_pool:vizdev"


def normalize_entity_concept_links(links: Any) -> list[str]:
    if not links or not isinstance(links, (list, tuple)):
        return []
    out: list[str] = []
    for lid in links:
        eid = str(lid).strip() if lid is not None else ""
        if eid and eid not in out:
            out.append(eid)
    return out


CONCEPT_ENTITY_MODEL_PER_KITSU_CONCEPT = "per_kitsu_concept"
CONCEPT_ENTITY_MODEL_PER_LINKED_ENTITY = "per_linked_entity"


def concept_entity_model_is_per_linked_entity(concept_sync: Any) -> bool:
    """True when studio settings use one AYON folder per ``entity_concept_links`` target."""
    if concept_sync is None:
        return False
    if isinstance(concept_sync, dict):
        return (
            str(concept_sync.get("concept_entity_model") or "").strip()
            == CONCEPT_ENTITY_MODEL_PER_LINKED_ENTITY
        )
    return (
        str(getattr(concept_sync, "concept_entity_model", "") or "").strip()
        == CONCEPT_ENTITY_MODEL_PER_LINKED_ENTITY
    )


def _name_looks_kitsu_auto_generated(name: str) -> bool:
    """True when Kitsu kept an import-style ``name`` (file + UUID or numeric prefix)."""
    s = (name or "").strip()
    if not s:
        return False
    if _KITSU_CONCEPT_NAME_UUID_RE.match(s):
        return True
    if _KITSU_CONCEPT_LEADING_NUMERIC_PREFIX_RE.match(s):
        return True
    return False


def _joined_linked_entity_names(entity: dict[str, Any]) -> str | None:
    """Kitsu's concept grid shows linked entities' ``name`` (see cgwire/kitsu ConceptCard).

    Callers may set ``linked_entity_names`` to a list of strings (same order as
    ``entity_concept_links``). Returns a single display string or ``None``.
    """
    raw = entity.get("linked_entity_names")
    if not isinstance(raw, list) or not raw:
        return None
    parts: list[str] = []
    for item in raw:
        s = str(item).strip() if item is not None else ""
        if s and s not in parts:
            parts.append(s)
    if not parts:
        return None
    return ", ".join(parts)


def concept_folder_base_slug(display_label: str) -> str:
    """Slug from the human label only (no uniqueness; used for relink candidates)."""
    label = (display_label or "").strip() or "concept"
    return slugify(label, separator="_") or "concept"


def concept_primary_title_for_folder(entity: dict[str, Any]) -> str:
    """Pick the Kitsu string that should drive the AYON Concept folder **label**.

    On **create**, folder **slugs** are allocated in ``utils.allocate_unique_concept_folder_name_label``
    (``base``, ``base_2``, … under the same parent). Existing folders are not renamed on
    each sync.

    Order:
      1. ``linked_entity_names`` when present (Kitsu UI parity with linked tags).
      2. Otherwise ``name`` / ``code`` heuristics for auto-import vs editorial renames.
    """
    linked = _joined_linked_entity_names(entity)
    if linked:
        return linked

    name = (entity.get("name") or "").strip()
    code = (entity.get("code") or "").strip()
    name_auto = _name_looks_kitsu_auto_generated(name)
    code_auto = _name_looks_kitsu_auto_generated(code)
    if code and name_auto:
        return code
    if name and code_auto:
        return name
    if name:
        return name
    return code


def concept_folder_display_name(kitsu_name: str, *, sanitize: bool) -> str:
    """Human-readable folder label for Kitsu Concept; ``data.kitsuId`` stays the concept id."""
    raw = (kitsu_name or "").strip()
    # Kitsu / macOS can inject narrow no-break space into screenshot filenames; strip for regexes.
    raw = raw.replace("\u202f", " ").replace("\u00a0", " ")
    if not raw:
        return "concept"
    if not sanitize:
        return raw
    m = _KITSU_CONCEPT_NAME_UUID_RE.match(raw)
    if m:
        stem = (m.group(1) or "").strip()
        if stem:
            return stem
    m2 = _KITSU_CONCEPT_LEADING_NUMERIC_PREFIX_RE.match(raw)
    if m2:
        tail = (m2.group(2) or "").strip()
        if tail:
            return concept_folder_display_name(tail, sanitize=True)
    return raw
