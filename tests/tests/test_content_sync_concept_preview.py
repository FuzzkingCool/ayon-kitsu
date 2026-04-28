"""Unit tests for concept preview routing (VizDev surrogate, no Kitsu task)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from ayon_api.exceptions import HTTPRequestError

from processor import content_sync


def test_concept_vizdev_surrogate_roundtrip():
    cid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    sur = content_sync.concept_vizdev_surrogate_kitsu_id(cid)
    assert sur == f"kitsu:concept:{cid}:vizdev"
    assert content_sync._concept_id_from_vizdev_surrogate_task_id(sur) == cid


def test_sync_preview_concept_surrogate_resolves_vizdev_task(monkeypatch):
    """Surrogate task_id skips gazu.task.get_task and uses folder + VizDev child."""
    concept_id = "concept-uuid-1111-2222-3333-444444444444"
    preview_id = "preview-file-id-1"
    project_id = "kitsu-proj-1"
    folder_id = "ayon-folder-abc"
    vizdev_task_id = "ayon-task-vizdev"

    processor = MagicMock()
    processor.get_paired_ayon_project.return_value = "TestProject"
    processor.kitsu_server_url = "http://kitsu.test/api"
    processor.settings = {}

    monkeypatch.setattr(content_sync.processor_utils, "set_kitsu_host", lambda url: None)

    preview = {
        "id": preview_id,
        "revision": 1,
        "comment_id": "",
        "extension": "png",
        "original_name": "concept.png",
        "status": "ready",
        "position": 0,
    }
    monkeypatch.setattr(
        content_sync.gazu.files,
        "get_preview_file",
        lambda pid: preview if pid == preview_id else None,
    )

    gazu_task_called = {"n": 0}

    def no_get_task(_tid):
        gazu_task_called["n"] += 1
        raise AssertionError("gazu.task.get_task must not run for surrogate task_id")

    monkeypatch.setattr(content_sync.gazu.task, "get_task", no_get_task)

    monkeypatch.setattr(
        content_sync,
        "_ayon_folder_by_kitsu_id",
        lambda pn, kid: {"id": folder_id} if kid == concept_id else None,
    )

    vizdev_task = {
        "id": vizdev_task_id,
        "folderId": folder_id,
        "data": {
            "kitsuId": content_sync.concept_vizdev_surrogate_kitsu_id(concept_id),
        },
    }

    monkeypatch.setattr(
        content_sync,
        "_ayon_task_by_kitsu_id",
        lambda pn, kid: None,
    )

    surrogate = content_sync.concept_vizdev_surrogate_kitsu_id(concept_id)

    monkeypatch.setattr(
        content_sync,
        "_ayon_vizdev_task_by_surrogate",
        lambda pn, fk, sur: (
            vizdev_task
            if fk == concept_id and sur == surrogate
            else None
        ),
    )

    product_id = "product-1"
    version_id = "version-1"
    monkeypatch.setattr(
        content_sync,
        "_find_or_create_review_product",
        lambda pn, fid, ttn, ktid, **kwargs: product_id,
    )
    monkeypatch.setattr(
        content_sync,
        "_find_or_create_review_version",
        lambda pn, pid, rev, ktid, cid, atid: version_id,
    )

    monkeypatch.setattr(
        content_sync.ayon_api,
        "get_version_by_id",
        lambda pn, vid: {"id": vid, "data": {}},
        raising=False,
    )

    uploaded = []

    def capture_upload(pn, vid, path, label=None, filename=None, content_type=None, **kwargs):
        uploaded.append(
            {
                "version_id": vid,
                "label": label,
                "filename": filename,
                "content_type": content_type,
            },
        )

    monkeypatch.setattr(
        content_sync.ayon_api,
        "upload_reviewable",
        capture_upload,
        raising=False,
    )

    def download_writes_png_header(url, path):
        Path(path).write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01",
        )

    monkeypatch.setattr(content_sync.gazu.client, "download", download_writes_png_header)
    meta_calls: list[tuple] = []

    def capture_meta(*args, **kwargs):
        meta_calls.append((args, kwargs))

    monkeypatch.setattr(
        content_sync,
        "_merge_kitsu_preview_metadata_on_version",
        capture_meta,
    )
    monkeypatch.setattr(
        content_sync,
        "_merge_kitsu_preview_file_ids_on_version",
        lambda *a, **k: None,
    )

    content_sync.sync_preview_to_ayon(
        processor, preview_id, surrogate, project_id,
    )

    assert gazu_task_called["n"] == 0
    assert uploaded, "expected main reviewable upload"
    main = [u for u in uploaded if u["filename"] and u["filename"].endswith(".png")]
    assert main, uploaded
    assert main[0]["version_id"] == version_id
    assert main[0]["label"] is None
    assert "_" in main[0]["filename"] and main[0]["filename"].endswith(".png")
    assert main[0]["content_type"] == "image/png"
    assert len(meta_calls) == 1
    (args0, kwargs0) = meta_calls[0]
    _pn, _vid, pid, _ann, pvf = args0
    assert pid == preview_id
    assert pvf["id"] == preview_id
    assert kwargs0.get("project_file_id") is None
    assert kwargs0.get("preview_kind") is None


def test_sync_preview_pdf_uploads_project_file_not_reviewable(monkeypatch):
    """Kitsu PDF preview bytes go to upload_project_file; version.data gets projectFileId."""
    concept_id = "concept-uuid-pdf-2222-3333-444444444444"
    preview_id = "preview-pdf-id-1"
    project_id = "kitsu-proj-pdf"
    folder_id = "ayon-folder-pdf"
    vizdev_task_id = "ayon-task-vizdev-pdf"

    processor = MagicMock()
    processor.get_paired_ayon_project.return_value = "TestProject"
    processor.kitsu_server_url = "http://kitsu.test/api"
    processor.settings = {}

    monkeypatch.setattr(content_sync.processor_utils, "set_kitsu_host", lambda url: None)

    preview = {
        "id": preview_id,
        "revision": 1,
        "comment_id": "",
        "extension": "pdf",
        "original_name": "Elder Kettle Revived Family Reunion - 20250904",
        "status": "ready",
        "position": 0,
    }
    monkeypatch.setattr(
        content_sync.gazu.files,
        "get_preview_file",
        lambda pid: preview if pid == preview_id else None,
    )
    monkeypatch.setattr(content_sync.gazu.task, "get_task", lambda _tid: None)

    monkeypatch.setattr(
        content_sync,
        "_ayon_folder_by_kitsu_id",
        lambda pn, kid: {"id": folder_id} if kid == concept_id else None,
    )

    surrogate = content_sync.concept_vizdev_surrogate_kitsu_id(concept_id)
    vizdev_task = {
        "id": vizdev_task_id,
        "folderId": folder_id,
        "data": {"kitsuId": surrogate},
    }
    monkeypatch.setattr(
        content_sync,
        "_ayon_task_by_kitsu_id",
        lambda pn, kid: None,
    )
    monkeypatch.setattr(
        content_sync,
        "_ayon_vizdev_task_by_surrogate",
        lambda pn, fk, sur: (
            vizdev_task if fk == concept_id and sur == surrogate else None
        ),
    )

    version_id = "version-pdf-1"
    monkeypatch.setattr(
        content_sync,
        "_find_or_create_review_product",
        lambda pn, fid, ttn, ktid, **kwargs: "product-pdf-1",
    )
    monkeypatch.setattr(
        content_sync,
        "_find_or_create_review_version",
        lambda pn, pid, rev, ktid, cid, atid: version_id,
    )

    ver_state: dict = {"data": {}}

    def fake_get_version(pn, vid):
        return {"id": vid, "data": dict(ver_state["data"])}

    def fake_update_version(pn, vid, data=None, **kwargs):
        if data:
            inner = dict(ver_state["data"])
            for k, v in data.items():
                inner[k] = v
            ver_state["data"] = inner

    monkeypatch.setattr(
        content_sync.ayon_api,
        "get_version_by_id",
        fake_get_version,
        raising=False,
    )
    monkeypatch.setattr(
        content_sync.ayon_api,
        "update_version",
        fake_update_version,
        raising=False,
    )

    reviewable_calls: list = []

    def capture_reviewable(*a, **k):
        reviewable_calls.append((a, k))

    monkeypatch.setattr(
        content_sync.ayon_api,
        "upload_reviewable",
        capture_reviewable,
        raising=False,
    )

    project_file_calls: list = []

    def capture_project_file(pn, path, filename=None, **kwargs):
        project_file_calls.append({"filename": filename, "path": path})
        m = MagicMock()
        m.json.return_value = {"id": "ayon-file-pdf-1"}
        return m

    monkeypatch.setattr(
        content_sync.ayon_api,
        "upload_project_file",
        capture_project_file,
        raising=False,
    )

    def download_writes_pdf(url, path):
        Path(path).write_bytes(b"%PDF-1.3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF")

    monkeypatch.setattr(content_sync.gazu.client, "download", download_writes_pdf)
    monkeypatch.setattr(
        content_sync,
        "_merge_kitsu_preview_file_ids_on_version",
        lambda *a, **k: None,
    )

    content_sync.sync_preview_to_ayon(
        processor, preview_id, surrogate, project_id,
    )

    assert not reviewable_calls
    assert len(project_file_calls) == 1
    assert project_file_calls[0]["filename"].endswith(".pdf")
    art = ver_state["data"]["kitsuPreviewArtifacts"][preview_id]
    assert art["projectFileId"] == "ayon-file-pdf-1"
    assert art["previewKind"] == "pdf"
    assert art["previewFile"]["id"] == preview_id


def test_sync_preview_reviewable_extract_failure_falls_back_to_project_file(monkeypatch):
    """AYON 400 Failed to extract media info -> same bytes via upload_project_file."""
    concept_id = "concept-uuid-extract-3333-444444444444"
    preview_id = "preview-extract-fail-1"
    project_id = "kitsu-proj-ex"
    folder_id = "ayon-folder-ex"
    vizdev_task_id = "ayon-task-vizdev-ex"

    processor = MagicMock()
    processor.get_paired_ayon_project.return_value = "TestProject"
    processor.kitsu_server_url = "http://kitsu.test/api"
    processor.settings = {}

    monkeypatch.setattr(content_sync.processor_utils, "set_kitsu_host", lambda url: None)

    preview = {
        "id": preview_id,
        "revision": 1,
        "comment_id": "",
        "extension": "png",
        "original_name": "still.png",
        "status": "ready",
        "position": 0,
    }
    monkeypatch.setattr(
        content_sync.gazu.files,
        "get_preview_file",
        lambda pid: preview if pid == preview_id else None,
    )
    monkeypatch.setattr(content_sync.gazu.task, "get_task", lambda _tid: None)

    monkeypatch.setattr(
        content_sync,
        "_ayon_folder_by_kitsu_id",
        lambda pn, kid: {"id": folder_id} if kid == concept_id else None,
    )

    surrogate = content_sync.concept_vizdev_surrogate_kitsu_id(concept_id)
    vizdev_task = {
        "id": vizdev_task_id,
        "folderId": folder_id,
        "data": {"kitsuId": surrogate},
    }
    monkeypatch.setattr(
        content_sync,
        "_ayon_task_by_kitsu_id",
        lambda pn, kid: None,
    )
    monkeypatch.setattr(
        content_sync,
        "_ayon_vizdev_task_by_surrogate",
        lambda pn, fk, sur: (
            vizdev_task if fk == concept_id and sur == surrogate else None
        ),
    )

    monkeypatch.setattr(
        content_sync,
        "_find_or_create_review_product",
        lambda pn, fid, ttn, ktid, **kwargs: "product-ex-1",
    )
    monkeypatch.setattr(
        content_sync,
        "_find_or_create_review_version",
        lambda pn, pid, rev, ktid, cid, atid: "version-ex-1",
    )

    ver_state: dict = {"data": {}}

    def fake_get_version(pn, vid):
        return {"id": vid, "data": dict(ver_state["data"])}

    def fake_update_version(pn, vid, data=None, **kwargs):
        if data:
            inner = dict(ver_state["data"])
            for k, v in data.items():
                inner[k] = v
            ver_state["data"] = inner

    monkeypatch.setattr(
        content_sync.ayon_api,
        "get_version_by_id",
        fake_get_version,
        raising=False,
    )
    monkeypatch.setattr(
        content_sync.ayon_api,
        "update_version",
        fake_update_version,
        raising=False,
    )

    def reviewable_raises_extract(_pn, _vid, _path, **_kwargs):
        resp = MagicMock()
        resp.status_code = 400
        resp.json.return_value = {
            "code": 400,
            "detail": "Failed to extract media info",
        }
        resp.text = '{"code":400,"detail":"Failed to extract media info"}'
        raise HTTPRequestError("400 Bad Request", response=resp)

    monkeypatch.setattr(
        content_sync.ayon_api,
        "upload_reviewable",
        reviewable_raises_extract,
        raising=False,
    )

    project_file_calls: list = []

    def capture_project_file(pn, path, filename=None, **kwargs):
        project_file_calls.append({"filename": filename, "path": path})
        m = MagicMock()
        m.json.return_value = {"id": "ayon-file-fallback-1"}
        return m

    monkeypatch.setattr(
        content_sync.ayon_api,
        "upload_project_file",
        capture_project_file,
        raising=False,
    )

    def download_writes_png(url, path):
        Path(path).write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01",
        )

    monkeypatch.setattr(content_sync.gazu.client, "download", download_writes_png)
    monkeypatch.setattr(
        content_sync,
        "_merge_kitsu_preview_file_ids_on_version",
        lambda *a, **k: None,
    )

    content_sync.sync_preview_to_ayon(
        processor, preview_id, surrogate, project_id,
    )

    assert len(project_file_calls) == 1
    assert project_file_calls[0]["filename"].endswith(".png")
    art = ver_state["data"]["kitsuPreviewArtifacts"][preview_id]
    assert art["projectFileId"] == "ayon-file-fallback-1"
    assert art["previewKind"] == "extract_failed_sidecar"
    assert art["previewFile"]["id"] == preview_id


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", None),
        ("not-a-surrogate", None),
        ("kitsu:concept:x:vizdev", "x"),
    ],
)
def test_concept_id_from_surrogate_parsing(raw, expected):
    assert content_sync._concept_id_from_vizdev_surrogate_task_id(raw) == expected


def test_linked_entity_id_from_surrogate_parsing():
    lid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    sur = content_sync.concept_vizdev_surrogate_for_linked_entity(lid)
    assert sur == f"kitsu:link:{lid}:vizdev"
    assert content_sync._linked_entity_id_from_vizdev_surrogate_task_id(sur) == lid


def test_per_linked_concept_review_product_names_differ(monkeypatch):
    monkeypatch.setattr(
        content_sync,
        "slugify",
        lambda value, separator="_": str(value).replace("-", separator)[:48],
    )
    a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    pf = "preview-file-uuid-000000000000"
    na = content_sync._per_linked_concept_review_product_name(
        "VizDev", source_kitsu_concept_id=a, preview_file_id=pf,
    )
    nb = content_sync._per_linked_concept_review_product_name(
        "VizDev", source_kitsu_concept_id=b, preview_file_id=pf,
    )
    assert na != nb
    assert na.startswith("vizdevKitsuReview_c_")
    assert nb.startswith("vizdevKitsuReview_c_")


def test_sync_preview_link_surrogate_uses_per_concept_product(monkeypatch):
    """Merged link folder: each Kitsu concept preview maps to its own review product."""
    linked_entity_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    source_concept_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    preview_id = "preview-file-id-link-1"
    project_id = "kitsu-proj-1"
    folder_id = "ayon-folder-linked"
    vizdev_task_id = "ayon-task-vizdev-link"

    processor = MagicMock()
    processor.get_paired_ayon_project.return_value = "TestProject"
    processor.kitsu_server_url = "http://kitsu.test/api"
    processor.settings = {}

    monkeypatch.setattr(content_sync.processor_utils, "set_kitsu_host", lambda url: None)

    preview = {
        "id": preview_id,
        "revision": 1,
        "comment_id": "",
        "extension": "png",
        "original_name": "concept.png",
        "status": "ready",
        "position": 0,
    }
    monkeypatch.setattr(
        content_sync.gazu.files,
        "get_preview_file",
        lambda pid: preview if pid == preview_id else None,
    )
    monkeypatch.setattr(content_sync.gazu.task, "get_task", lambda _tid: None)

    monkeypatch.setattr(
        content_sync,
        "_ayon_folder_by_kitsu_id",
        lambda pn, kid: {"id": folder_id} if kid == linked_entity_id else None,
    )

    surrogate = content_sync.concept_vizdev_surrogate_for_linked_entity(
        linked_entity_id,
    )
    vizdev_task = {
        "id": vizdev_task_id,
        "folderId": folder_id,
        "data": {"kitsuId": surrogate},
    }
    monkeypatch.setattr(
        content_sync,
        "_ayon_task_by_kitsu_id",
        lambda pn, kid: None,
    )
    monkeypatch.setattr(
        content_sync,
        "_ayon_vizdev_task_by_surrogate",
        lambda pn, fk, sur: (
            vizdev_task
            if fk == linked_entity_id and sur == surrogate
            else None
        ),
    )

    product_calls: list[dict] = []

    def capture_product(pn, fid, ttn, ktid, **kwargs):
        product_calls.append(kwargs)
        return "product-link-1"

    monkeypatch.setattr(
        content_sync,
        "_find_or_create_review_product",
        capture_product,
    )
    monkeypatch.setattr(
        content_sync,
        "_find_or_create_review_version",
        lambda pn, pid, rev, ktid, cid, atid: "version-link-1",
    )
    monkeypatch.setattr(
        content_sync.ayon_api,
        "get_version_by_id",
        lambda pn, vid: {"id": vid, "data": {}},
        raising=False,
    )
    monkeypatch.setattr(
        content_sync.ayon_api,
        "upload_reviewable",
        lambda *a, **k: None,
        raising=False,
    )
    monkeypatch.setattr(content_sync.gazu.client, "download", lambda url, path: None)
    monkeypatch.setattr(
        content_sync,
        "_merge_kitsu_preview_file_ids_on_version",
        lambda *a, **k: None,
    )

    content_sync.sync_preview_to_ayon(
        processor,
        preview_id,
        surrogate,
        project_id,
        source_kitsu_concept_id=source_concept_id,
    )

    assert len(product_calls) == 1
    kw0 = product_calls[0]
    assert kw0.get("product_name")
    assert kw0["product_name"] == content_sync._per_linked_concept_review_product_name(
        "VizDev",
        source_kitsu_concept_id=source_concept_id,
        preview_file_id=preview_id,
    )
    extra = kw0.get("product_data_extra") or {}
    assert extra.get("kitsuSourceConceptId") == source_concept_id
