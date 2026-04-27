"""Unit tests for Concept orphan folder relink (server/kitsu/push.py)."""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if sys.version_info < (3, 10):
    pytest.skip("push.py uses match statements (Python 3.10+)", allow_module_level=True)

import push


def test_try_relink_orphan_concept_folder_sets_kitsu_id():
    async def _body():
        user = MagicMock()
        project = MagicMock(name="TestProj")
        raw = "temp1.png-5f893bd8-69eb-49d7-9093-fb4cc8327894"
        entity_dict = {
            "type": "Concept",
            "id": "concept-uuid-1",
            "name": raw,
            "parent_id": None,
        }
        existing: dict = {}
        legacy_slug = push.slugify(raw, separator="_")

        mock_folder = MagicMock()
        mock_folder.data = {}
        mock_folder.id = "ayon-folder-id"
        mock_folder.name = legacy_slug
        mock_folder.parent_id = "concepts-root-id"
        mock_folder.save = AsyncMock()

        with patch.object(push, "get_root_folder_id", new_callable=AsyncMock) as m_root:
            m_root.return_value = "concepts-root-id"
            with patch.object(push.Postgres, "fetch", new_callable=AsyncMock) as m_fetch:
                m_fetch.return_value = [{"id": "orphan-folder-id", "data": {}}]
                with patch.object(
                    push.FolderEntity, "load", new_callable=AsyncMock
                ) as m_load:
                    m_load.return_value = mock_folder
                    with patch.object(push, "dispatch_event", new_callable=AsyncMock):
                        ok = await push.try_relink_orphan_concept_folder(
                            user, project, entity_dict, existing
                        )

        assert ok is True
        assert existing["concept-uuid-1"] == "ayon-folder-id"
        assert mock_folder.data["kitsuId"] == "concept-uuid-1"
        mock_folder.save.assert_called_once()
        m_fetch.assert_awaited_once()
        assert m_fetch.await_args.args[1] == "concepts-root-id"
        assert legacy_slug in m_fetch.await_args.args[2]

    asyncio.run(_body())


def test_try_relink_skips_when_kitsu_parent_not_none():
    async def _body():
        user = MagicMock()
        project = MagicMock(name="TestProj")
        entity_dict = {
            "type": "Concept",
            "id": "c1",
            "name": "x.png-uuid",
            "parent_id": "some-parent",
        }
        with patch.object(push.Postgres, "fetch", new_callable=AsyncMock) as m_fetch:
            ok = await push.try_relink_orphan_concept_folder(
                user, project, entity_dict, {}
            )
        assert ok is False
        m_fetch.assert_not_called()

    asyncio.run(_body())


def test_try_relink_ambiguous_returns_false():
    async def _body():
        user = MagicMock()
        project = MagicMock(name="P")
        entity_dict = {
            "type": "Concept",
            "id": "c1",
            "name": "same.png-uuid",
            "parent_id": None,
        }
        with patch.object(push, "get_root_folder_id", new_callable=AsyncMock) as m_root:
            m_root.return_value = "root"
            with patch.object(push.Postgres, "fetch", new_callable=AsyncMock) as m_fetch:
                m_fetch.return_value = [
                    {"id": "a", "data": {}},
                    {"id": "b", "data": {}},
                ]
                with patch.object(
                    push.FolderEntity, "load", new_callable=AsyncMock
                ) as m_load:
                    ok = await push.try_relink_orphan_concept_folder(
                        user, project, entity_dict, {}
                    )
        assert ok is False
        m_load.assert_not_called()

    asyncio.run(_body())
