"""Unit tests for content_sync browser URL helpers."""

from __future__ import annotations

import pytest

from processor import content_sync_browser_urls as u


@pytest.mark.parametrize(
    ("api_url", "expected_base"),
    [
        ("https://kitsu.example.com/api", "https://kitsu.example.com"),
        ("https://kitsu.example.com/api/", "https://kitsu.example.com"),
        ("https://kitsu.example.com", "https://kitsu.example.com"),
    ],
)
def test_kitsu_ui_base_url_strips_api(api_url: str, expected_base: str) -> None:
    assert u.kitsu_ui_base_url(api_url) == expected_base


def test_kitsu_browser_url_task_shots_vs_assets() -> None:
    base = "https://kitsu.example.com/api"
    pid = "proj-1"
    tid = "task-99"
    url_shot = u.kitsu_browser_url(
        base,
        pid,
        kitsu_task_id=tid,
        folder_type_for_entity="Shot",
    )
    assert url_shot.endswith(f"/productions/{pid}/shots/tasks/{tid}")
    url_asset = u.kitsu_browser_url(
        base,
        pid,
        kitsu_task_id=tid,
        folder_type_for_entity="Character",
    )
    assert url_asset.endswith(f"/productions/{pid}/assets/tasks/{tid}")


def test_kitsu_browser_url_entity_path() -> None:
    base = "https://kitsu.example.com/api"
    pid = "proj-1"
    eid = "ent-7"
    url = u.kitsu_browser_url(
        base,
        pid,
        kitsu_entity_id=eid,
        folder_type_for_entity="Sequence",
    )
    assert url.endswith(f"/productions/{pid}/shots/{eid}")


def test_ayon_browser_url_project_overview_encodes_segment() -> None:
    url = u.ayon_browser_url_project_overview(
        "https://ayon.test/",
        "My Project",
    )
    assert url == "https://ayon.test/projects/My%20Project/overview"


def test_ayon_browser_url_task_context_task_uses_details_panel_query() -> None:
    url = u.ayon_browser_url_task_context(
        "https://ayon.test",
        "demo",
        ayon_task_id="tid-1",
        ayon_folder_id="fid-2",
    )
    assert url.startswith("https://ayon.test/projects/demo/tasks?")
    assert "project=demo" in url
    assert "type=task" in url
    assert "id=tid-1" in url
    assert "task=" not in url
    assert "folder=" not in url


def test_ayon_browser_url_task_context_studio_golden_not_legacy_task_folder() -> None:
    """Regression: old ``?task=&folder=`` links do not select in the web UI."""
    url = u.ayon_browser_url_task_context(
        "https://studio.ayon.app",
        "ABC_Edits",
        ayon_task_id="648b4d5010ec11f19f9a8279983dccb2",
        ayon_folder_id="64464e3010ec11f19f9a8279983dccb2",
    )
    assert url == (
        "https://studio.ayon.app/projects/ABC_Edits/tasks?"
        "project=ABC_Edits&type=task&id=648b4d5010ec11f19f9a8279983dccb2"
    )


def test_ayon_browser_url_task_context_folder_only() -> None:
    url = u.ayon_browser_url_task_context(
        "https://ayon.test",
        "demo",
        ayon_task_id=None,
        ayon_folder_id="fid-only",
    )
    assert "type=folder" in url
    assert "id=fid-only" in url
    assert "project=demo" in url


def test_ayon_browser_url_workfiles_prefers_task_over_folder() -> None:
    url = u.ayon_browser_url_workfiles(
        "https://ayon.test",
        "demo",
        ayon_folder_id="fid",
        ayon_task_id="tid",
    )
    assert "/projects/demo/workfiles?" in url
    assert "type=task" in url
    assert "id=tid" in url
    assert "project=demo" in url
    assert "task=" not in url
    assert "folder=" not in url


def test_ayon_browser_url_workfiles_folder_only() -> None:
    url = u.ayon_browser_url_workfiles(
        "https://ayon.test",
        "demo",
        ayon_folder_id="fid",
        ayon_task_id=None,
    )
    assert "type=folder" in url
    assert "id=fid" in url
