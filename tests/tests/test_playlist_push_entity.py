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
import processor.playlist_push_entity as ppe
from processor.playlist_push_entity import (
    build_playlist_push_entity,
    ordered_kitsu_member_ids_for_push,
    sync_playlists_via_push_for_project,
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


def test_sync_playlists_skips_post_when_no_resolved_members(monkeypatch):
    posted: list[int] = []

    class _Ok:
        ok = True
        status_code = 200
        text = ""
        url = ""

        def raise_for_status(self):
            return None

    def fake_post(*_a, **_k):
        posted.append(1)
        return _Ok()

    monkeypatch.setattr(ppe.ayon_api, "post", fake_post, raising=False)
    monkeypatch.setattr(
        ppe.ayon_api, "get_base_url", lambda: "https://ayon.test", raising=False,
    )
    monkeypatch.setattr(
        ppe,
        "iter_playlists_for_kitsu_project",
        lambda _k: [{"id": "pl-empty"}],
    )
    monkeypatch.setattr(
        ppe.gazu.playlist,
        "get_playlist",
        lambda pl_id: {"id": pl_id, "name": "No Shots", "shots": []},
    )
    proc = MagicMock()
    proc.settings = {"sync_settings": {"playlist_sync": {"enabled": True}}}
    proc.entrypoint = "https://ayon"
    stats = sync_playlists_via_push_for_project(
        proc, "kitsu-proj", "ayon_proj", {},
    )
    assert stats["fetched"] == 1
    assert stats["pushed"] == 0
    assert stats["zero_members"] == 1
    assert posted == []


def test_sync_playlists_posts_when_push_when_zero_members_true(monkeypatch):
    posted: list[int] = []

    class _Ok:
        ok = True
        status_code = 200
        text = ""
        url = ""

        def raise_for_status(self):
            return None

    def fake_post(*_a, **_k):
        posted.append(1)
        return _Ok()

    monkeypatch.setattr(ppe.ayon_api, "post", fake_post, raising=False)
    monkeypatch.setattr(
        ppe.ayon_api, "get_base_url", lambda: "https://ayon.test", raising=False,
    )
    monkeypatch.setattr(
        ppe,
        "iter_playlists_for_kitsu_project",
        lambda _k: [{"id": "pl-z"}],
    )
    monkeypatch.setattr(
        ppe.gazu.playlist,
        "get_playlist",
        lambda pl_id: {"id": pl_id, "name": "Z", "shots": []},
    )
    proc = MagicMock()
    proc.settings = {
        "sync_settings": {
            "playlist_sync": {
                "enabled": True,
                "push_when_zero_members": True,
            },
        },
    }
    proc.entrypoint = "https://ayon"
    stats = sync_playlists_via_push_for_project(proc, "kp", "ayon_proj", {})
    assert stats["pushed"] == 1
    assert stats["zero_members"] == 1
    assert len(posted) == 1


def test_retryable_playlist_post_error_500_when_response_suggests_transient():
    from ayon_api.exceptions import HTTPRequestError

    resp = MagicMock()
    resp.status_code = 500
    resp.text = '{"detail":"Bad Gateway from lists"}'
    exc = HTTPRequestError("push failed", response=resp)
    assert ppe._retryable_playlist_post_error(exc) is True


def test_retryable_playlist_post_error_500_generic_not_retryable():
    from ayon_api.exceptions import HTTPRequestError

    resp = MagicMock()
    resp.status_code = 500
    resp.text = '{"detail":"Internal validation failed"}'
    exc = HTTPRequestError("push failed", response=resp)
    assert ppe._retryable_playlist_post_error(exc) is False


def test_post_playlist_entities_retries_on_connection_error(monkeypatch):
    posted: list[int] = []

    class _Ok:
        ok = True
        status_code = 200
        text = ""
        url = ""

        def raise_for_status(self):
            return None

    def fake_post(*_a, **_k):
        posted.append(1)
        if len(posted) < 2:
            raise ConnectionError("reset")
        return _Ok()

    monkeypatch.setattr(ppe.ayon_api, "post", fake_post, raising=False)
    monkeypatch.setattr(ppe.time, "sleep", lambda *_a, **_k: None)
    proc = MagicMock()
    proc.entrypoint = "https://ayon"
    ppe._post_playlist_entities(proc, "ayon_proj", [{"type": "Playlist", "id": "pl1"}])
    assert len(posted) == 2
