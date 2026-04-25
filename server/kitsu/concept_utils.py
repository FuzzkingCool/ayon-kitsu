"""Pure helpers for Kitsu Concept → AYON display naming and VizDev surrogate ids."""

from __future__ import annotations

import re

# Kitsu often names concepts ``<stem>.<ext>-<uuid>`` (see tests mock_data).
_KITSU_CONCEPT_NAME_UUID_RE = re.compile(
    r"^(.+)(\.[A-Za-z0-9]{1,10})-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)


def concept_vizdev_surrogate_kitsu_id(concept_id: str) -> str:
    """Deterministic id for the AYON-only VizDev task; must not be a real Kitsu UUID."""
    return f"kitsu:concept:{concept_id}:vizdev"


def concept_folder_display_name(kitsu_name: str, *, sanitize: bool) -> str:
    """Human-readable folder label for Kitsu Concept; ``data.kitsuId`` stays the concept id."""
    raw = (kitsu_name or "").strip()
    if not raw:
        return "concept"
    if not sanitize:
        return raw
    m = _KITSU_CONCEPT_NAME_UUID_RE.match(raw)
    if m:
        stem = (m.group(1) or "").strip()
        if stem:
            return stem
    return raw
