"""Tests for processor.sync_error_format headline and reason helpers."""

from processor.sync_error_format import (
    enrich_summary_for_emit,
    error_code_from_payload,
    format_batch_push_headline,
    format_entity_sync_headline,
    format_partial_sync_headline,
    short_reason_from_payload,
)
from processor.sync_events import parse_http_error_detail
from ayon_api.exceptions import ServerError


def test_short_reason_unique_violation_with_detail():
    p = {
        "error": "unique-violation",
        "detail": "Task with folder_id, name 'x, tiedown' already exists.",
    }
    r = short_reason_from_payload(p)
    assert "duplicate record" in r
    assert "tiedown" in r


def test_short_reason_timeout_message():
    p = {"message": "500 Server Error: Connection timed out."}
    assert "timeout" in short_reason_from_payload(p).lower()


def test_short_reason_server_error_parse():
    p = parse_http_error_detail(ServerError("Connection timed out."))
    assert p.get("detail") == "Connection timed out."
    assert "timeout" in short_reason_from_payload(p).lower()


def test_format_entity_headline():
    p = {
        "error": "unique-violation",
        "detail": "Folder with parent_id, name 'p, char_x' already exists.",
    }
    h = format_entity_sync_headline(
        "MyGame", "Asset", "CHAR_blueDudePowerDash", "d324e176-4fdd-4603-8b32-854fa8e08bce", p
    )
    assert h.startswith("MyGame | Asset CHAR_blueDudePowerDash")
    assert "Kitsu d324e176…" in h
    assert "duplicate record" in h


def test_format_batch_headline():
    p = {"error": "unique-violation", "detail": "batch conflict"}
    h = format_batch_push_headline(
        "MyGame", 3, 40, {"Asset": 100}, p
    )
    assert "MyGame" in h
    assert "Batch 3/40" in h
    assert "Asset:100" in h
    assert "one-by-one" in h


def test_format_partial_sync_headline():
    h = format_partial_sync_headline("P", 2, 100)
    assert "P" in h
    assert "2" in h and "100" in h


def test_error_code_from_payload():
    assert error_code_from_payload({"error": "unique-violation"}) == "unique-violation"
    assert "http" in error_code_from_payload({"httpStatus": 500}).lower()


def test_enrich_summary_for_emit():
    payload = {"error": "unique-violation", "detail": "x"}
    s = enrich_summary_for_emit(
        "MyProj",
        {"phase": "individual_push", "kitsuEntityId": "kid"},
        payload,
    )
    assert s["projectName"] == "MyProj"
    assert s["phase"] == "individual_push"
    assert s["shortReason"]
    assert s["errorCode"] == "unique-violation"
