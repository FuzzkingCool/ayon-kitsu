"""Unit tests for processor task relink and sync event helpers."""

from unittest.mock import MagicMock, patch

import pytest

from processor.task_relink import (
    is_task_unique_violation,
    merge_push_response_folder_map,
    push_entities_with_relink,
    try_relink_stale_kitsu_task,
)
from processor.sync_events import parse_http_error_detail


def test_merge_push_response_folder_map():
    acc = {"a": "1"}
    merge_push_response_folder_map(acc, {"folders": {"b": "2", "c": "3"}})
    assert acc == {"a": "1", "b": "2", "c": "3"}
    merge_push_response_folder_map(acc, None)
    merge_push_response_folder_map(acc, {})
    assert acc == {"a": "1", "b": "2", "c": "3"}


def test_is_task_unique_violation_string():
    assert is_task_unique_violation(
        Exception("unique-violation on tasks")
    )
    assert is_task_unique_violation(
        Exception("Task with folder_id, name 'x, y' already exists.")
    )


def test_is_task_unique_violation_response_json():
    resp = MagicMock()

    def json():
        return {"detail": "already exists.", "error": "unique-violation"}

    resp.json = json
    exc = type("E", (Exception,), {})("fail")
    exc.response = resp
    assert is_task_unique_violation(exc)


@patch("processor.task_relink.ayon_api.update_task", create=True)
@patch("processor.task_relink.ayon_api.get_tasks_by_folder_path", create=True)
@patch("processor.task_relink.ayon_api.get_folder_by_id", create=True)
def test_try_relink_stale_kitsu_task_updates(
    mock_get_folder, mock_get_tasks, mock_update
):
    mock_get_folder.return_value = {"id": "f1", "path": "/assets/chars/foo"}
    mock_get_tasks.return_value = [
        {
            "id": "t1",
            "name": "modeling",
            "taskType": "Modeling",
            "data": {"kitsuId": "old-kitsu-uuid"},
        }
    ]
    task_entity = {
        "type": "Task",
        "id": "new-kitsu-uuid",
        "entity_id": "asset-kitsu-id",
        "name": "modeling",
        "task_type_name": "Modeling",
    }
    folder_map = {"asset-kitsu-id": "f1"}
    assert try_relink_stale_kitsu_task("Proj", task_entity, folder_map) is True
    mock_update.assert_called_once()
    call_kw = mock_update.call_args
    assert call_kw[0][0] == "Proj"
    assert call_kw[0][1] == "t1"
    assert call_kw[1]["data"]["kitsuId"] == "new-kitsu-uuid"


@patch("processor.task_relink.ayon_api.update_task", create=True)
@patch("processor.task_relink.ayon_api.get_tasks_by_folder_path", create=True)
@patch("processor.task_relink.ayon_api.get_folder_by_id", create=True)
def test_try_relink_skips_when_multiple_matches(
    mock_get_folder, mock_get_tasks, mock_update
):
    mock_get_folder.return_value = {"id": "f1", "path": "/x"}
    mock_get_tasks.return_value = [{"id": "1"}, {"id": "2"}]
    task_entity = {
        "type": "Task",
        "id": "n",
        "entity_id": "e",
        "name": "a",
        "task_type_name": "T",
    }
    assert try_relink_stale_kitsu_task("P", task_entity, {"e": "f1"}) is False
    mock_update.assert_not_called()


@patch("processor.task_relink.try_relink_stale_kitsu_task")
@patch("processor.task_relink.ayon_api.post")
def test_push_entities_with_relink_retries(mock_post, mock_relink):
    mock_relink.return_value = True
    ok = MagicMock()
    ok.raise_for_status = MagicMock()
    ok.data = {"folders": {}}
    fail = MagicMock()

    def rs():
        if mock_post.call_count == 1:
            raise RuntimeError("unique-violation")
        return None

    fail.raise_for_status = rs
    mock_post.side_effect = [fail, ok]

    task = {
        "type": "Task",
        "id": "kid",
        "entity_id": "eid",
        "name": "tiedown",
        "task_type_name": "TieDown",
    }
    push_entities_with_relink("/addons/kitsu/x", "MyProj", [task], {})
    assert mock_post.call_count == 2
    mock_relink.assert_called_once()


@patch("processor.sync_events.ayon_api.update_event", create=True)
@patch("processor.sync_events.ayon_api.create_event", create=True)
def test_emit_sync_entity_failed_swallows_errors(mock_create, mock_update):
    from processor.sync_events import emit_sync_entity_failed

    mock_create.side_effect = RuntimeError("network")
    emit_sync_entity_failed("P", "desc", {"k": "v"})
    mock_update.assert_not_called()


def test_parse_http_error_detail_plain():
    d = parse_http_error_detail(ValueError("x"))
    assert d["message"] == "x"
