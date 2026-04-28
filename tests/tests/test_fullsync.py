"""Unit tests for ``processor.fullsync`` helpers (gazu mocked via ``mock_data``)."""

import gazu
from processor import fullsync

from . import mock_data

PROJECT_ID = "kitsu-project-id-1"
PROJECT_NAME = "test_kitsu_project"


def test_get_asset_types(monkeypatch):
    monkeypatch.setattr(
        gazu.asset,
        "all_asset_types_for_project",
        lambda x: mock_data.all_asset_types_for_project,
    )
    res = fullsync.get_asset_types(PROJECT_ID)

    assert res == {
        "asset-type-id-1": "Character",
        "asset-type-id-2": "Rig",
        "asset-type-id-3": "Location",
    }


def test_get_task_types(monkeypatch):
    monkeypatch.setattr(
        gazu.task,
        "all_task_types_for_project",
        lambda x: mock_data.all_task_types_for_project,
    )
    res = fullsync.get_task_types(PROJECT_ID)
    assert res == {"task-type-id-1": "Animation", "task-type-id-2": "Compositing"}


def test_get_statuses(monkeypatch):
    monkeypatch.setattr(
        gazu.task,
        "all_task_statuses",
        lambda: mock_data.all_task_statuses,
    )
    res = fullsync.get_statuses()
    assert res == {"task-status-id-1": "Todo", "task-status-id-2": "Approved"}


def test_get_assets(monkeypatch):
    monkeypatch.setattr(
        gazu.asset,
        "all_assets_for_project",
        lambda x: mock_data.all_assets_for_project,
    )
    res = fullsync.get_assets(
        PROJECT_ID,
        {
            "asset-type-id-1": "Character2",
            "asset-type-id-2": "Rig2",
            "asset-type-id-3": "Location2",
        },
    )
    assert len(res) == 2
    assert res[0]["id"] == "asset-id-1"
    assert res[0]["asset_type_name"] == "Character2"

    assert res[1]["id"] == "asset-id-2"
    assert res[1]["asset_type_name"] == "Rig2"


def test_get_tasks(monkeypatch):
    monkeypatch.setattr(
        gazu.task,
        "all_tasks_for_project",
        lambda x: mock_data.all_tasks_for_project,
    )
    monkeypatch.setattr(
        gazu.person,
        "get_person",
        lambda x: mock_data.all_persons[0],
    )
    persons_by_id = {p["id"]: p for p in mock_data.all_persons}
    ayon_users_by_email: dict[str, str] = {}
    res = fullsync.get_tasks(
        PROJECT_ID,
        {"task-type-id-1": "Animation", "task-type-id-2": "Compositing"},
        {"task-status-id-1": "Todo", "task-status-id-2": "Approved"},
        persons_by_id,
        ayon_users_by_email,
    )
    assert len(res) == 2
    assert res[0]["id"] == "task-id-1"
    assert res[1]["id"] == "task-id-2"
