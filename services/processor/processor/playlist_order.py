"""Pure helpers for Kitsu playlist row ordering (no ayon_api / gazu imports)."""

from __future__ import annotations

from typing import Any


def ordered_kitsu_entity_ids_from_playlist(playlist: dict[str, Any]) -> list[str]:
    """Return Kitsu ``entity_id`` values in playlist order (shots table rows)."""
    rows = playlist.get("shots") or []
    if not isinstance(rows, list):
        return []
    out: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        eid = row.get("entity_id") or row.get("shot_id") or row.get("object_id")
        if eid:
            out.append(str(eid))
    return out
