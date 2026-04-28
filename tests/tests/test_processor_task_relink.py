"""Unit tests for processor task relink and sync event helpers."""

from unittest.mock import MagicMock, patch

import pytest

from processor.task_relink import (
    is_folder_unique_violation,
    is_task_unique_violation,
    kitsu_folder_map_from_ayon_project,
    merge_push_response_folder_map,
    push_entities_with_relink,
    try_relink_stale_kitsu_asset_folder,
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


@patch("processor.task_relink.ayon_api.get_folders", create=True)
def test_kitsu_folder_map_from_ayon_project(mock_get_folders):
    mock_get_folders.return_value = [
        {"id": "ay-1", "data": {"kitsuId": "kitsu-shot-1"}},
        {"id": "ay-2", "data": {}},
        {"id": "ay-3", "data": {"kitsuId": "kitsu-shot-2"}},
    ]
    m = kitsu_folder_map_from_ayon_project("demo")
    mock_get_folders.assert_called_once_with("demo", active=True)
    assert m == {"kitsu-shot-1": "ay-1", "kitsu-shot-2": "ay-3"}


def test_is_task_unique_violation_string():
    assert is_task_unique_violation(
        Exception("unique-violation on tasks")
    )
    assert is_task_unique_violation(
        Exception("Task with folder_id, name 'x, y' already exists.")
    )


def test_is_folder_unique_violation_string():
    assert is_folder_unique_violation(
        Exception(
            "Folder with parent_id, name 'uuid, char_foo' already exists."
        )
    )


def test_is_folder_unique_violation_response_json():
    resp = MagicMock()

    def json():
        return {
            "detail": "Folder with parent_id, name 'x, y' already exists.",
            "error": "unique-violation",
        }

    resp.json = json
    exc = type("E", (Exception,), {})("fail")
    exc.response = resp
    assert is_folder_unique_violation(exc)


def test_is_folder_unique_violation_not_task_only():
    assert not is_folder_unique_violation(
        Exception("Task with folder_id, name 'a, b' already exists.")
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


@patch("processor.task_relink.ayon_api.update_folder", create=True)
@patch("processor.task_relink.ayon_api.get_folders", create=True)
def test_try_relink_stale_kitsu_asset_folder_updates(
    mock_get_folders, mock_update
):
    mock_get_folders.return_value = [
        {
            "id": "fold1",
            "parentId": "type_parent",
            "name": "char_bluedudepowerdash",
            "data": {"kitsuId": "old-asset-uuid"},
        }
    ]
    asset = {
        "type": "Asset",
        "id": "new-kitsu-uuid",
        "entity_type_id": "etype-1",
        "name": "CHAR_blueDudePowerDash",
    }
    assert (
        try_relink_stale_kitsu_asset_folder(
            "Proj", asset, {"etype-1": "type_parent"}
        )
        is True
    )
    mock_update.assert_called_once()
    assert mock_update.call_args[0][0] == "Proj"
    assert mock_update.call_args[0][1] == "fold1"
    assert mock_update.call_args[1]["data"]["kitsuId"] == "new-kitsu-uuid"


@patch("processor.task_relink.ayon_api.update_folder", create=True)
@patch("processor.task_relink.ayon_api.get_folders", create=True)
def test_try_relink_asset_skips_when_kitsu_id_matches(
    mock_get_folders, mock_update
):
    mock_get_folders.return_value = [
        {
            "id": "f1",
            "parentId": "p1",
            "name": "char_x",
            "data": {"kitsuId": "same-uuid"},
        }
    ]
    asset = {
        "type": "Asset",
        "id": "same-uuid",
        "entity_type_id": "e1",
        "name": "CHAR_X",
    }
    assert try_relink_stale_kitsu_asset_folder("P", asset, {"e1": "p1"}) is False
    mock_update.assert_not_called()


@patch("processor.task_relink.try_relink_stale_kitsu_asset_folder")
@patch("processor.task_relink.ayon_api.post")
def test_push_entities_with_relink_retries_asset(mock_post, mock_relink_asset):
    mock_relink_asset.return_value = True
    ok = MagicMock()
    ok.raise_for_status = MagicMock()
    ok.data = {"folders": {}}
    fail = MagicMock()

    def rs():
        if mock_post.call_count == 1:
            raise RuntimeError(
                "Folder with parent_id, name 'a, b' already exists."
            )
        return None

    fail.raise_for_status = rs
    mock_post.side_effect = [fail, ok]

    asset = {
        "type": "Asset",
        "id": "kid",
        "entity_type_id": "etype",
        "name": "CHAR_Foo",
    }
    push_entities_with_relink("/addons/kitsu/x", "MyProj", [asset], {"etype": "par"})
    assert mock_post.call_count == 2
    mock_relink_asset.assert_called_once()


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
    assert mock_create.call_count == 3


def test_parse_http_error_detail_plain():
    d = parse_http_error_detail(ValueError("x"))
    assert d["message"] == "x"
