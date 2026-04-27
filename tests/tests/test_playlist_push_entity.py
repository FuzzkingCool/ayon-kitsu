"""Unit tests for playlist → /push entity builder."""

import sys
from unittest.mock import MagicMock

sys.modules.setdefault("ayon_api", MagicMock())
sys.modules.setdefault("gazu", MagicMock())
_nxt = MagicMock()

def _slugify(s, **kwargs):
    return s.replace(" ", "_")

_nxt.slugify = _slugify
_nxt.logging = MagicMock()
sys.modules.setdefault("nxtools", _nxt)

from processor.playlist_order import ordered_kitsu_entity_ids_from_playlist
from processor.playlist_push_entity import (
    build_playlist_push_entity,
    ordered_kitsu_member_ids_for_push,
)


def test_ordered_kitsu_entity_ids_from_playlist_preserves_order():
    pl = {
        "id": "pl1",
        "shots": [
            {"entity_id": "a", "preview_file_id": "p1"},
            {"entity_id": "b", "preview_file_id": "p2"},
        ],
    }
    assert ordered_kitsu_entity_ids_from_playlist(pl) == ["a", "b"]


def test_ordered_kitsu_entity_ids_skips_bad_rows():
    pl = {"id": "pl1", "shots": [{}, "x", {"entity_id": "z"}]}
    assert ordered_kitsu_entity_ids_from_playlist(pl) == ["z"]


def test_ordered_kitsu_entity_ids_shot_id_column():
    pl = {"id": "pl1", "shots": [{"shot_id": "s1"}, {"object_id": "s2"}]}
    assert ordered_kitsu_entity_ids_from_playlist(pl) == ["s1", "s2"]


def test_ordered_kitsu_member_ids_uses_all_shots_fallback(monkeypatch):
    pl = {"id": "pl-fb", "shots": []}

    def fake_all_shots(p):
        assert p["id"] == "pl-fb"
        return [{"id": "shot-x"}, {"id": "shot-y"}]

    import processor.playlist_push_entity as ppe

    monkeypatch.setattr(ppe.gazu.playlist, "all_shots_for_playlist", fake_all_shots)
    assert ordered_kitsu_member_ids_for_push(pl) == ["shot-x", "shot-y"]


def test_build_playlist_push_entity_resolves_and_skips_unknown():
    playlist = {
        "id": "kpl-1",
        "name": " Review ",
        "shots": [{"entity_id": "shot-a"}, {"entity_id": "orphan"}],
    }
    folder_map = {"shot-a": "ayon-folder-1"}
    ent = build_playlist_push_entity(
        "demo_project", playlist, folder_map, "https://ayon.test"
    )
    assert ent["type"] == "Playlist"
    assert ent["id"] == "kpl-1"
    assert ent["name"] == "Review"
    assert ent["ayon_server_url"] == "https://ayon.test"
    assert ent["ordered_ayon_folder_ids"] == ["ayon-folder-1"]
