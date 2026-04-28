"""Kitsu Concept → AYON folder title resolution (kept in sync with server ``concept_utils``).

The processor OCI image does not ship the AYON **server** addon. Studio hosts may run
a server build that still uses naive ``name``-only folder labels. We normalize the
Concept payload *before* ``POST /push`` so the entity ``name`` is the same **primary
title** the server would compute (``code`` over auto-generated ``name``), and the
server's ``sync_folder`` / ``concept_primary_title_for_folder`` + sanitize path
receives the right string every time.
"""

from __future__ import annotations

import re
from typing import Any

# Mirror server/kitsu/concept_utils.py
_KITSU_CONCEPT_NAME_UUID_RE = re.compile(
    r"^(.+)(\.[A-Za-z0-9]{1,15})-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
_KITSU_CONCEPT_LEADING_NUMERIC_PREFIX_RE = re.compile(r"^(\d{6,})-(.+)$")


def _name_looks_kitsu_auto_generated(name: str) -> bool:
    s = (name or "").strip()
    if not s:
        return False
    if _KITSU_CONCEPT_NAME_UUID_RE.match(s):
        return True
    if _KITSU_CONCEPT_LEADING_NUMERIC_PREFIX_RE.match(s):
        return True
    return False


def _joined_linked_entity_names(entity: dict[str, Any]) -> str | None:
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


def concept_primary_title_for_folder(entity: dict[str, Any]) -> str:
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


def apply_concept_title_for_ayon_push(entity: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with ``name`` set to the resolved primary Kitsu title for folder sync.

    Kitsu can keep a long import-style ``name`` while the editor title lives in
    ``code``. Pushing the resolved string as ``name`` makes older server builds
    still create/update the right AYON folder label; newer servers apply the
    same rule in ``sync_folder``.

    When ``linked_entity_names`` is set (Kitsu concept card parity), it is kept
    on the payload so the server can derive the same title without extra Kitsu
    round-trips.
    """
    out = dict(entity)
    out.setdefault("type", "Concept")
    out["name"] = concept_primary_title_for_folder(out)
    return out
