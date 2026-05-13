"""Unit tests for playlist_list_coercion (stdlib only; no ayon_server)."""

import playlist_list_coercion as plc


def test_coerce_entity_list_data_dict_roundtrip():
    assert plc.coerce_entity_list_data({"kitsuId": "abc"}) == {"kitsuId": "abc"}


def test_coerce_entity_list_data_json_string():
    raw = '{"kitsuId": "pl-1", "kitsuSource": "playlist"}'
    assert plc.coerce_entity_list_data(raw) == {
        "kitsuId": "pl-1",
        "kitsuSource": "playlist",
    }


def test_coerce_entity_list_data_invalid_json():
    assert plc.coerce_entity_list_data("{not json") == {}


def test_coerce_entity_list_data_none():
    assert plc.coerce_entity_list_data(None) == {}


def test_kitsu_id_match_candidates_plain():
    assert plc.kitsu_id_match_candidates("abc-123") == ["abc-123"]


def test_kitsu_id_match_candidates_uuid_variants():
    u = "550e8400-e29b-41d4-a716-446655440000"
    out = plc.kitsu_id_match_candidates(u)
    assert u in out
    assert "550e8400e29b41d4a716446655440000" in out


def test_kitsu_id_match_candidates_empty():
    assert plc.kitsu_id_match_candidates("") == []
    assert plc.kitsu_id_match_candidates("   ") == []
    assert plc.kitsu_id_match_candidates(None) == []
