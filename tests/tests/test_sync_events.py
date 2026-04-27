"""Tests for processor sync event emission and HTTP error parsing."""

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("ayon_api")

from processor.sync_events import (
    emit_sync_entity_failed,
    emit_sync_summary,
    parse_http_error_detail,
)


def test_parse_http_error_detail_http_request_error_500_unique_violation():
    from ayon_api.exceptions import HTTPRequestError

    # Path shape matches AYON addon push URL; do not tie tests to repo version.
    example_push_path = "[POST] /addons/kitsu/0.0.0-mock/push"

    resp = MagicMock()
    resp.status_code = 500
    resp.json = MagicMock(
        return_value={
            "path": example_push_path,
            "file": "asyncpg/protocol/protocol.pyx",
            "detail": (
                "Task with folder_id, name "
                "'23d9f0aa-f093-11f0-a170-f6e17cbebbb3, fx' already exists."
            ),
            "error": "unique-violation",
            "code": 409,
        }
    )
    exc = HTTPRequestError(
        "500 Server Error: Internal Server Error for url: https://example/push",
        response=resp,
    )
    d = parse_http_error_detail(exc)
    assert d["httpStatus"] == 500
    assert d["error"] == "unique-violation"
    assert d["code"] == 409
    assert "already exists" in d["detail"]
    assert d["path"] == example_push_path


@patch("processor.sync_events.time.sleep", autospec=True)
@patch("processor.sync_events.ayon_api.update_event")
@patch("processor.sync_events.ayon_api.create_event")
def test_emit_sync_entity_failed_retries_then_succeeds(
    mock_create, mock_update, mock_sleep
):
    mock_create.side_effect = [RuntimeError("transient"), "evt-99"]
    emit_sync_entity_failed("MyProj", "headline", {"phase": "batch_push"})

    assert mock_create.call_count == 2
    assert mock_update.call_count == 1
    assert mock_update.call_args[0][0] == "evt-99"
    assert mock_update.call_args[1]["project_name"] == "MyProj"
    assert mock_update.call_args[1]["status"] == "failed"
    assert mock_sleep.call_count == 1


@patch("processor.sync_events.time.sleep", autospec=True)
@patch("processor.sync_events.ayon_api.update_event")
@patch("processor.sync_events.ayon_api.create_event")
def test_emit_sync_entity_failed_exhausts_retries(mock_create, mock_update, mock_sleep):
    mock_create.side_effect = RuntimeError("always down")
    emit_sync_entity_failed("P", "d" * 300, {"k": "v"})

    assert mock_create.call_count == 3
    mock_update.assert_not_called()
    assert mock_sleep.call_count == 2


@patch("processor.sync_events.time.sleep", autospec=True)
@patch("processor.sync_events.ayon_api.update_event")
@patch("processor.sync_events.ayon_api.create_event")
def test_emit_sync_summary_retries_then_succeeds(
    mock_create, mock_update, mock_sleep
):
    mock_create.side_effect = [RuntimeError("transient"), "sum-1"]
    emit_sync_summary("ProjB", "recovered headline", {"phase": "batch_push_recovered"})

    assert mock_create.call_count == 2
    assert mock_update.call_count == 1
    assert mock_update.call_args[0][0] == "sum-1"
    assert mock_update.call_args[1]["status"] == "finished"
    assert mock_sleep.call_count == 1
