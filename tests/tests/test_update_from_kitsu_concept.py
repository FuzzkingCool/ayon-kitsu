"""Unit test: concept update merges canonical ``get_concept`` over list API row."""

from unittest.mock import MagicMock

import gazu
from processor import content_sync as processor_content_sync
from processor import update_from_kitsu

PROJECT_ID = "kitsu-project-id-1"
PROJECT_NAME = "test_kitsu_project"


def test_create_or_update_concept_merges_canonical_get_concept_over_list_row(
    monkeypatch,
):
    """List row is used for discovery; ``get_concept`` overlays name/code for push."""
    import ayon_api

    monkeypatch.setattr(ayon_api, "get_base_url", lambda: "http://ayon.test/")
    monkeypatch.setattr(
        processor_content_sync,
        "sync_preview_to_ayon",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        processor_content_sync,
        "sync_thumbnail_to_ayon",
        lambda *args, **kwargs: None,
    )

    processor = MagicMock()
    processor.entrypoint = "addons/kitsu/mock"
    processor.kitsu_server_url = "http://kitsu.test/api"
    processor.get_paired_ayon_project.return_value = PROJECT_NAME

    captured: dict = {}

    def fake_push(entrypoint, project_name, entities, folder_map):
        captured["entities"] = list(entities)

        class Resp:
            status_code = 200
            data: dict = {"folders": {}, "tasks": {}}

            def raise_for_status(self):
                pass

        return Resp()

    monkeypatch.setattr(update_from_kitsu, "push_entities_with_relink", fake_push)

    official = {
        "id": "concept-list-id",
        "name": "StaleFromList",
        "type": "Concept",
        "project_id": PROJECT_ID,
        "parent_id": None,
    }

    def fake_client_get(path, json_response=True, params=None, client=None):
        if path == "data/concepts" and params and params.get("project_id") == PROJECT_ID:
            return [official]
        raise AssertionError(f"unexpected gazu.client.get {path!r} {params!r}")

    monkeypatch.setattr(gazu.client, "get", fake_client_get)
    monkeypatch.setattr(
        gazu.concept,
        "get_concept",
        lambda cid: {
            "id": cid,
            "name": "CanonicalFreshTitle",
            "type": "Concept",
            "project_id": PROJECT_ID,
        },
    )

    update_from_kitsu.create_or_update_concept(
        processor,
        {"concept_id": "concept-list-id", "project_id": PROJECT_ID},
    )
    assert captured["entities"][0]["name"] == "CanonicalFreshTitle"
