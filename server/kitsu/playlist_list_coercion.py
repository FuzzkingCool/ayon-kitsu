"""Pure helpers for Kitsu playlist id / entity list ``data`` normalization.

Kept free of ``ayon_server`` imports so unit tests can run in lightweight CI.
"""

from __future__ import annotations

import json
import uuid
from typing import Any


def coerce_entity_list_data(raw: Any) -> dict:
    """Normalize ``data`` from Postgres (dict or JSON string) to a ``dict``."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except Exception:
            return {}
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def kitsu_id_match_candidates(kitsu_playlist_id: str | None) -> list[str]:
    """String forms that may appear in ``data->>'kitsuId'`` for the same playlist."""
    if kitsu_playlist_id is None:
        return []
    s = str(kitsu_playlist_id).strip()
    if not s:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(x: str) -> None:
        if x and x not in seen:
            seen.add(x)
            out.append(x)

    add(s)
    sl = s.lower()
    h = sl.replace("-", "")
    if len(h) == 32 and all(c in "0123456789abcdef" for c in h):
        try:
            dashed = str(uuid.UUID(h))
            add(dashed)
            add(h)
        except ValueError:
            add(h)
    return out
