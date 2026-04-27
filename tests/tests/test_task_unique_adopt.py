"""Unit tests for task unique-violation adoption helpers (server/kitsu/utils.py)."""

from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("ayon_server")

import utils


def test_is_task_folder_name_unique_violation_positive():
    exc = Exception(
        "Task with folder_id, name '23d9f0aa-f093-11f0-a170-f6e17cbebbb3, fx' already exists."
    )
    assert utils.is_task_folder_name_unique_violation(exc) is True


def test_is_task_folder_name_unique_violation_negative():
    assert utils.is_task_folder_name_unique_violation(Exception("folder conflict")) is False


@pytest.mark.asyncio
async def test_find_task_id_by_folder_name_type_single():
    with patch.object(utils.Postgres, "fetch", new_callable=AsyncMock) as m_fetch:
        m_fetch.return_value = [{"id": "task-1"}]
        out = await utils.find_task_id_by_folder_name_type(
            "MyProject", "folder-a", "fx", "Animation"
        )
        assert out == "task-1"


@pytest.mark.asyncio
async def test_find_task_id_by_folder_name_type_ambiguous():
    with patch.object(utils.Postgres, "fetch", new_callable=AsyncMock) as m_fetch:
        m_fetch.return_value = [{"id": "a"}, {"id": "b"}]
        out = await utils.find_task_id_by_folder_name_type(
            "MyProject", "folder-a", "fx", "Animation"
        )
        assert out is None
