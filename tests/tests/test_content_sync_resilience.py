"""Tests for content_sync GraphQL resilience and reviewable label sanitization."""

import importlib.util
import re
import sys
import types
from typing import Any
from unittest.mock import MagicMock, patch

sys.modules.setdefault("gazu", MagicMock())


def _ensure_nxtools_stub() -> None:
    """Do not replace a real ``nxtools`` install (checklist tests need ``slugify``)."""
    try:
        if importlib.util.find_spec("nxtools") is not None:
            return
    except ValueError:
        pass
    _nxt = MagicMock()
    _nxt.logging = MagicMock()
    sys.modules["nxtools"] = _nxt


_ensure_nxtools_stub()


def _ensure_ayon_api_stub() -> None:
    """Processor imports ayon_api; CI may not install it — mirror test_playlist_push_entity."""
    try:
        if importlib.util.find_spec("ayon_api") is not None:
            return
    except ValueError:
        # Broken install (e.g. ayon_api.__spec__ is None): replace with stub.
        pass
    exc_mod = types.ModuleType("ayon_api.exceptions")

    class HTTPRequestError(Exception):
        def __init__(self, message="", response=None):
            super().__init__(message)
            self.response = response

    exc_mod.HTTPRequestError = HTTPRequestError
    sys.modules["ayon_api.exceptions"] = exc_mod
    ayon_mod = types.ModuleType("ayon_api")
    ayon_mod.exceptions = exc_mod
    ayon_mod.get_tasks = MagicMock(return_value=())
    ayon_mod.get_folders = MagicMock(return_value=())
    sys.modules["ayon_api"] = ayon_mod


_ensure_ayon_api_stub()


def _ensure_requests_stub() -> None:
    if importlib.util.find_spec("requests") is not None:
        return
    req_ex = types.ModuleType("requests.exceptions")

    class RequestException(Exception):
        pass

    class HTTPError(RequestException):
        def __init__(self, *args, response=None, **kwargs):
            super().__init__(*args, **kwargs)
            self.response = response

    class ConnectionError(RequestException):
        pass

    class Timeout(RequestException):
        pass

    class ChunkedEncodingError(RequestException):
        pass

    req_ex.RequestException = RequestException
    req_ex.HTTPError = HTTPError
    req_ex.ConnectionError = ConnectionError
    req_ex.Timeout = Timeout
    req_ex.ChunkedEncodingError = ChunkedEncodingError
    sys.modules["requests.exceptions"] = req_ex
    req_mod = types.ModuleType("requests")
    req_mod.exceptions = req_ex
    sys.modules["requests"] = req_mod


_ensure_requests_stub()

from ayon_api.exceptions import HTTPRequestError

from processor import content_sync


def test_reviewable_label_datetime_like_string():
    pid = "ec227a7a-d048-4816-8a5e-1b8b55a02ab9"
    out = content_sync._reviewable_label("2026-04-27 17:49:33", pid)
    assert " " not in out
    assert ":" not in out
    assert "ec227a7ad048" in out


def test_reviewable_label_windows_path_basename():
    pid = "abc"
    out = content_sync._reviewable_label(r"C:\foo\bar\MyShot.png", pid)
    assert "MyShot" in out
    assert "\\" not in out
    assert "." not in out


def test_reviewable_label_alphanumeric_and_underscore_only():
    out = content_sync._reviewable_label("weird !@# name.txt", "11111111-1111-1111-1111-111111111111")
    assert re.match(r"^[A-Za-z0-9_]+$", out)


def test_reviewable_upload_basename_includes_safe_extension():
    pid = "22222222-2222-2222-2222-222222222222"
    bn = content_sync._reviewable_upload_basename("clip.mov", pid, ".MP4")
    assert bn == content_sync._reviewable_label("clip.mov", pid) + ".mp4"


def test_preview_sidecar_base_strips_hyphens():
    uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    b = content_sync._preview_sidecar_base(3, uid)
    assert "-" not in b
    assert b.startswith("03_")
    assert "aaaaaaaa" in b


def test_sniff_reviewable_media_png(tmp_path):
    p = tmp_path / "raw.bin"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
    ext, mime = content_sync._sniff_reviewable_media(str(p))
    assert ext == "png"
    assert mime == "image/png"


def test_sniff_reviewable_media_mp4(tmp_path):
    p = tmp_path / "raw.bin"
    # minimal ftyp box brand isom
    p.write_bytes(
        b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso6mp41" + b"\x00" * 8,
    )
    ext, mime = content_sync._sniff_reviewable_media(str(p))
    assert ext == "mp4"
    assert mime == "video/mp4"


def test_infer_reviewable_prefers_sniff_over_kitsu_bin(tmp_path):
    p = tmp_path / "t.bin"
    p.write_bytes(
        b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso6mp41" + b"\x00" * 8,
    )
    preview = {
        "id": "pf1",
        "extension": "bin",
        "revision": 1,
        "comment_id": "",
        "original_name": "ignored.bin",
    }
    name, mime = content_sync._infer_reviewable_basename_and_mime(
        str(p), preview, "ignored.bin", "pf1", "bin",
    )
    assert mime == "video/mp4"
    assert name.endswith(".mp4")


def test_is_pdf_preview_by_extension(tmp_path):
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert content_sync._is_pdf_preview(str(p), "pdf") is True
    assert content_sync._is_pdf_preview(str(p), "PDF") is True
    assert content_sync._is_pdf_preview(str(p), "png") is False


def test_is_pdf_preview_by_magic(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"%PDF-1.3\n% fake trailer")
    assert content_sync._is_pdf_preview(str(p), "bin") is True


def test_merge_kitsu_preview_metadata_includes_pdf_fields():
    ver_data: dict[str, Any] = {}

    def fake_get(pn, vid):
        return {"id": vid, "data": dict(ver_data)}

    def fake_update(pn, vid, data=None, **kwargs):
        if data:
            inner = dict(ver_data)
            for k, v in data.items():
                inner[k] = v
            ver_data.clear()
            ver_data.update(inner)

    with patch.object(content_sync.ayon_api, "get_version_by_id", fake_get, create=True):
        with patch.object(content_sync.ayon_api, "update_version", fake_update, create=True):
            content_sync._merge_kitsu_preview_metadata_on_version(
                "P",
                "v1",
                "pid1",
                [],
                {"id": "pid1"},
                project_file_id="file-pdf-1",
                preview_kind="pdf",
            )
    art = ver_data["kitsuPreviewArtifacts"]["pid1"]
    assert art["projectFileId"] == "file-pdf-1"
    assert art["previewKind"] == "pdf"
    assert art["previewFile"]["id"] == "pid1"


def test_http_error_detail_contains_extract_media_message():
    resp = MagicMock()
    resp.json.return_value = {
        "code": 400,
        "detail": "Failed to extract media info",
    }
    resp.text = '{"code":400,"detail":"Failed to extract media info"}'
    exc = HTTPRequestError("400", response=resp)
    assert content_sync._http_error_detail_contains(
        exc, "Failed to extract media info",
    )
    assert not content_sync._http_error_detail_contains(exc, "not present")


def test_http_error_body_snippet_from_http_request_error():
    resp = MagicMock()
    resp.text = '{"detail":"bad"}'
    exc = HTTPRequestError("400", response=resp)
    snip = content_sync._http_error_body_snippet(exc)
    assert "response_body=" in snip
    assert "bad" in snip


def test_reviewable_label_empty_uses_preview_id():
    pid = "deadbeef-cafe-4000-8000-000000000001"
    out = content_sync._reviewable_label("", pid)
    assert "preview" in out.lower()
    assert len(out) <= content_sync._MAX_REVIEWABLE_LABEL_LEN


def test_ayon_task_by_kitsu_id_returns_none_on_get_tasks_502():
    resp = MagicMock()
    resp.status_code = 502
    exc = HTTPRequestError(
        "502 Bad Gateway for url: https://example/graphql",
        response=resp,
    )
    with patch.object(content_sync.ayon_api, "get_tasks", side_effect=exc):
        assert content_sync._ayon_task_by_kitsu_id("ODY_Game", "task-kitsu-id") is None


def test_ayon_folder_by_kitsu_id_returns_none_on_get_folders_503():
    resp = MagicMock()
    resp.status_code = 503
    exc = HTTPRequestError("503", response=resp)
    with patch.object(content_sync.ayon_api, "get_folders", side_effect=exc):
        assert content_sync._ayon_folder_by_kitsu_id("ODY_Game", "ent-id") is None


def test_split_comment_body_single_part_no_header():
    short = "hello"
    parts = content_sync._split_comment_body_for_ayon_activities(short)
    assert parts == [short]
    assert all(len(p) <= content_sync._MAX_AYON_ACTIVITY_BODY_CHARS for p in parts)


def test_split_comment_body_multipart_under_limit():
    long_body = "x" * 4500
    parts = content_sync._split_comment_body_for_ayon_activities(long_body)
    assert len(parts) >= 2
    for p in parts:
        assert len(p) <= content_sync._MAX_AYON_ACTIVITY_BODY_CHARS
    if len(parts) > 1:
        assert all("_(Part " in p for p in parts)
    stripped: list[str] = []
    for p in parts:
        stripped.append(p.split("\n\n", 1)[-1] if "_(Part " in p else p)
    assert "".join(stripped) == long_body


def test_existing_comment_sync_uptodate_with_sha_multipart():
    sha = content_sync._sha256_utf8("ab")
    matches = [
        {
            "activityId": "a1",
            "body": "_(Part 1/2)_\n\na",
            "data": {
                "kitsuCommentId": "cid",
                "kitsuCommentPart": 1,
                "kitsuCommentPartCount": 2,
                "kitsuCommentBodySha256": sha,
            },
        },
        {
            "activityId": "a2",
            "body": "_(Part 2/2)_\n\nb",
            "data": {
                "kitsuCommentId": "cid",
                "kitsuCommentPart": 2,
                "kitsuCommentPartCount": 2,
                "kitsuCommentBodySha256": sha,
            },
        },
    ]
    assert content_sync._existing_comment_sync_uptodate(matches, 2, sha, "ab")


def test_existing_comment_sync_legacy_single_no_sha_body_match():
    matches = [
        {"activityId": "a1", "body": "hello", "data": {"kitsuCommentId": "cid"}},
    ]
    sha = content_sync._sha256_utf8("x")
    assert content_sync._existing_comment_sync_uptodate(matches, 1, sha, "hello")


def test_sync_comment_to_ayon_multipart_creates_and_skips_on_second_call():
    from unittest.mock import MagicMock

    processor = MagicMock()
    processor.get_paired_ayon_project.return_value = "Proj"
    processor.kitsu_server_url = "http://kitsu"
    long_body = "y" * 3500
    comment = {
        "id": "kc1",
        "person_id": "p1",
        "task_status_id": None,
        "text": long_body,
        "checklist": [],
        "previews": [],
        "attachment_files": [],
        "created_at": None,
    }
    ayon_task = {"id": "at1", "data": {"kitsuId": "kt1"}}
    stored: list[dict] = []

    def fake_get_activities(project_name, entity_ids=None, activity_types=None):
        return list(stored)

    def fake_create_activity(
        project_name,
        entity_id=None,
        entity_type=None,
        activity_type=None,
        body=None,
        file_ids=None,
        timestamp=None,
        data=None,
    ):
        aid = f"act{len(stored)}"
        stored.append(
            {
                "activityId": aid,
                "body": body,
                "data": dict(data) if data else {},
            },
        )
        return aid

    def fake_delete_activity(project_name, activity_id):
        nonlocal stored
        stored = [x for x in stored if x.get("activityId") != activity_id]

    with patch.object(content_sync.processor_utils, "set_kitsu_host"):
        with patch.object(content_sync, "gazu") as m_gazu:
            m_gazu.task.get_comment.return_value = comment
            m_gazu.person.all_persons.return_value = [{"id": "p1", "full_name": "Bob"}]
            m_gazu.task.all_task_statuses.return_value = []
            with patch.object(content_sync, "_ayon_task_by_kitsu_id", return_value=ayon_task):
                with patch.object(
                    content_sync.ayon_api,
                    "get_activities",
                    side_effect=fake_get_activities,
                    create=True,
                ):
                    with patch.object(
                        content_sync.ayon_api,
                        "create_activity",
                        side_effect=fake_create_activity,
                        create=True,
                    ):
                        with patch.object(
                            content_sync.ayon_api,
                            "delete_activity",
                            side_effect=fake_delete_activity,
                            create=True,
                        ):
                            with patch.object(
                                content_sync,
                                "maybe_sync_checklist_subtasks_from_kitsu_comment",
                            ):
                                content_sync.sync_comment_to_ayon(
                                    processor, "kc1", "kt1", "pid",
                                )
    assert len(stored) >= 2
    for s in stored:
        assert len(s["body"]) <= content_sync._MAX_AYON_ACTIVITY_BODY_CHARS

    with patch.object(content_sync.processor_utils, "set_kitsu_host"):
        with patch.object(content_sync, "gazu") as m_gazu:
            m_gazu.task.get_comment.return_value = comment
            m_gazu.person.all_persons.return_value = [{"id": "p1", "full_name": "Bob"}]
            m_gazu.task.all_task_statuses.return_value = []
            with patch.object(content_sync, "_ayon_task_by_kitsu_id", return_value=ayon_task):
                with patch.object(
                    content_sync.ayon_api,
                    "get_activities",
                    side_effect=fake_get_activities,
                    create=True,
                ):
                    create_mock = MagicMock(side_effect=fake_create_activity)
                    with patch.object(
                        content_sync.ayon_api,
                        "create_activity",
                        create_mock,
                        create=True,
                    ):
                        with patch.object(
                            content_sync.ayon_api,
                            "delete_activity",
                            side_effect=fake_delete_activity,
                            create=True,
                        ):
                            with patch.object(
                                content_sync,
                                "maybe_sync_checklist_subtasks_from_kitsu_comment",
                            ):
                                content_sync.sync_comment_to_ayon(
                                    processor, "kc1", "kt1", "pid",
                                )
    create_mock.assert_not_called()


def test_sync_comment_to_ayon_uses_gazu_files_for_attachment_download():
    """Regression: attachment bytes must use gazu.files.download_attachment_file (not gazu.task)."""
    from unittest.mock import MagicMock

    processor = MagicMock()
    processor.get_paired_ayon_project.return_value = "Proj"
    processor.kitsu_server_url = "http://kitsu"
    att = {"id": "att1", "name": "doc.pdf", "extension": "pdf"}
    comment = {
        "id": "kc1",
        "person_id": "p1",
        "task_status_id": None,
        "text": "hello",
        "checklist": [],
        "previews": [],
        "attachment_files": [att],
        "created_at": None,
    }
    ayon_task = {"id": "at1", "data": {"kitsuId": "kt1"}}
    stored: list[dict] = []

    def fake_get_activities(project_name, entity_ids=None, activity_types=None):
        return list(stored)

    def fake_create_activity(
        project_name,
        entity_id=None,
        entity_type=None,
        activity_type=None,
        body=None,
        file_ids=None,
        timestamp=None,
        data=None,
    ):
        aid = f"act{len(stored)}"
        stored.append(
            {
                "activityId": aid,
                "body": body,
                "file_ids": list(file_ids) if file_ids else None,
                "data": dict(data) if data else {},
            },
        )
        return aid

    def fake_delete_activity(project_name, activity_id):
        nonlocal stored
        stored = [x for x in stored if x.get("activityId") != activity_id]

    def fake_upload(project_name, path, filename=None, **kwargs):
        m = MagicMock()
        m.json.return_value = {"id": "fid-upload-1"}
        return m

    with patch.object(content_sync.processor_utils, "set_kitsu_host"):
        with patch.object(content_sync, "gazu") as m_gazu:
            m_gazu.task.get_comment.return_value = comment
            m_gazu.person.all_persons.return_value = [{"id": "p1", "full_name": "Bob"}]
            m_gazu.task.all_task_statuses.return_value = []
            m_dl = MagicMock()
            m_gazu.files.download_attachment_file = m_dl
            with patch.object(content_sync, "_ayon_task_by_kitsu_id", return_value=ayon_task):
                with patch.object(
                    content_sync.ayon_api,
                    "get_activities",
                    side_effect=fake_get_activities,
                    create=True,
                ):
                    with patch.object(
                        content_sync.ayon_api,
                        "create_activity",
                        side_effect=fake_create_activity,
                        create=True,
                    ):
                        with patch.object(
                            content_sync.ayon_api,
                            "delete_activity",
                            side_effect=fake_delete_activity,
                            create=True,
                        ):
                            with patch.object(
                                content_sync.ayon_api,
                                "upload_project_file",
                                side_effect=fake_upload,
                                create=True,
                            ):
                                with patch.object(
                                    content_sync,
                                    "maybe_sync_checklist_subtasks_from_kitsu_comment",
                                ):
                                    content_sync.sync_comment_to_ayon(
                                        processor, "kc1", "kt1", "pid",
                                    )
    m_dl.assert_called_once()
    call_att, call_path = m_dl.call_args[0]
    assert call_att == att
    assert isinstance(call_path, str)
    assert any(
        row.get("file_ids") == ["fid-upload-1"] for row in stored
    ), f"expected uploaded file id in activity file_ids, got {stored}"


def test_build_comment_body_string_checklist_and_preview_rows():
    from unittest.mock import MagicMock

    comment = {
        "person_id": "p1",
        "task_status_id": "s1",
        "text": "Note",
        "checklist": ["plain string item", {"checked": True, "text": "dict item"}],
        "previews": ["preview-id-str", {"id": "p2", "position": 1, "original_name": "b.exr"}],
    }
    persons = {"p1": {"full_name": "Alice"}}
    statuses = {"s1": "WIP"}
    processor = MagicMock()
    processor.settings = {"sync_settings": {"content_sync": {}}}
    body = content_sync._build_comment_body(comment, persons, statuses, processor)
    assert "plain string item" in body
    assert "dict item" in body
    assert "preview-id-str" not in body
    assert "b.exr" not in body
    assert "Revision" not in body


def test_build_comment_body_informative_preview_manifest():
    from unittest.mock import MagicMock

    comment = {
        "person_id": "p1",
        "task_status_id": "s1",
        "text": "Hi",
        "checklist": [],
        "previews": [
            {
                "id": "pv1",
                "position": 0,
                "revision": 3,
                "original_name": "render.exr",
            },
        ],
    }
    persons = {"p1": {"full_name": "Alice"}}
    statuses = {"s1": "WIP"}
    processor = MagicMock()
    processor.settings = {"sync_settings": {"content_sync": {}}}
    body = content_sync._build_comment_body(comment, persons, statuses, processor)
    assert "**Revision 3**" in body
    assert "render.exr" in body


def test_build_comment_body_legacy_append_manifest_lists_thin_previews():
    from unittest.mock import MagicMock

    comment = {
        "person_id": "p1",
        "task_status_id": "s1",
        "text": "Hi",
        "checklist": [],
        "previews": ["only-id-string"],
    }
    persons = {"p1": {"full_name": "Alice"}}
    statuses = {"s1": "WIP"}
    processor = MagicMock()
    processor.settings = {
        "sync_settings": {
            "content_sync": {"append_kitsu_preview_manifest": True},
        },
    }
    body = content_sync._build_comment_body(comment, persons, statuses, processor)
    assert "only-id-string" in body
    assert "review files" in body


def test_build_comment_body_suppress_attribution_header():
    from unittest.mock import MagicMock

    comment = {
        "person_id": "p1",
        "task_status_id": "s1",
        "text": "Hi",
        "checklist": [],
        "previews": [],
    }
    persons = {"p1": {"full_name": "Alice"}}
    statuses = {"s1": "WIP"}
    processor = MagicMock()
    processor.settings = {"sync_settings": {"content_sync": {}}}
    body = content_sync._build_comment_body(
        comment, persons, statuses, processor, suppress_attribution_header=True,
    )
    assert "**[Alice]**" not in body
    assert "Hi" in body


def test_attach_comment_preview_json_sidecars_disabled_skips_preview_fetch():
    from unittest.mock import MagicMock

    processor = MagicMock()
    processor.get_paired_ayon_project.return_value = "Proj"
    processor.kitsu_server_url = "http://kitsu"
    processor.settings = {"sync_settings": {"content_sync": {}}}
    comment = {
        "id": "kc1",
        "person_id": "p1",
        "task_status_id": None,
        "text": "hello",
        "checklist": [],
        "previews": [{"id": "pv1", "position": 0, "original_name": "x.png"}],
        "attachment_files": [],
        "created_at": None,
    }
    ayon_task = {"id": "at1", "data": {"kitsuId": "kt1"}}
    stored: list[dict] = []

    def fake_get_activities(project_name, entity_ids=None, activity_types=None):
        return list(stored)

    def fake_create_activity(_pn, **kwargs):
        aid = f"act{len(stored)}"
        stored.append({"activityId": aid, "body": kwargs.get("body"), "data": dict(kwargs.get("data") or {})})
        return aid

    with patch.object(content_sync.processor_utils, "set_kitsu_host"):
        with patch.object(content_sync, "gazu") as m_gazu:
            m_gazu.task.get_comment.return_value = comment
            m_gazu.person.all_persons.return_value = [{"id": "p1", "full_name": "Bob", "email": "b@x.dev"}]
            m_gazu.task.all_task_statuses.return_value = []
            m_gpf = MagicMock()
            m_gazu.files.get_preview_file = m_gpf
            with patch.object(content_sync, "_ayon_task_by_kitsu_id", return_value=ayon_task):
                with patch.object(
                    content_sync.ayon_api,
                    "get_activities",
                    side_effect=fake_get_activities,
                    create=True,
                ):
                    with patch.object(
                        content_sync.ayon_api,
                        "create_activity",
                        side_effect=fake_create_activity,
                        create=True,
                    ):
                        with patch.object(
                            content_sync.ayon_api,
                            "delete_activity",
                            create=True,
                        ):
                            with patch.object(
                                content_sync,
                                "maybe_sync_checklist_subtasks_from_kitsu_comment",
                            ):
                                con = MagicMock()
                                con.is_service_user.return_value = False
                                with patch.object(
                                    content_sync.ayon_api,
                                    "get_server_api_connection",
                                    return_value=con,
                                    create=True,
                                ):
                                    content_sync.sync_comment_to_ayon(
                                        processor, "kc1", "kt1", "pid",
                                    )
    m_gpf.assert_not_called()
    assert stored, "expected one activity"
    assert "JSON files are attached" not in (stored[0].get("body") or "")


def test_comment_sync_impersonation_uses_as_username_when_service():
    from unittest.mock import MagicMock

    processor = MagicMock()
    processor.get_paired_ayon_project.return_value = "Proj"
    processor.kitsu_server_url = "http://kitsu"
    processor.settings = {"sync_settings": {"content_sync": {}}}
    comment = {
        "id": "kc1",
        "person_id": "p1",
        "task_status_id": None,
        "text": "hello",
        "checklist": [],
        "previews": [],
        "attachment_files": [],
        "created_at": None,
    }
    ayon_task = {"id": "at1", "data": {"kitsuId": "kt1"}}
    # Stale row forces delete + recreate; body mismatch vs new "hello" sync so not uptodate.
    stored: list[dict] = [
        {
            "activityId": "stale1",
            "body": "old-body",
            "data": {"kitsuCommentId": "kc1", "kitsuCommentBodySha256": "deadbeef"},
        },
    ]
    call_order: list[str] = []

    def fake_get_activities(project_name, entity_ids=None, activity_types=None):
        return list(stored)

    def fake_create_activity(_pn, **kwargs):
        call_order.append("create_activity")
        aid = f"act{len(stored)}"
        stored.append({"activityId": aid, "body": kwargs.get("body"), "data": dict(kwargs.get("data") or {})})
        return aid

    def fake_delete_activity(project_name, activity_id):
        call_order.append("delete_activity")
        idx = next(i for i, x in enumerate(stored) if x.get("activityId") == activity_id)
        stored.pop(idx)

    ctx = MagicMock()
    ctx.__exit__ = MagicMock(return_value=None)

    def on_enter():
        call_order.append("as_username_enter")
        return None

    ctx.__enter__ = MagicMock(side_effect=on_enter)
    con = MagicMock()
    con.is_service_user.return_value = True
    con.as_username.return_value = ctx

    with patch.object(content_sync.processor_utils, "set_kitsu_host"):
        with patch.object(content_sync, "gazu") as m_gazu:
            m_gazu.task.get_comment.return_value = comment
            m_gazu.person.all_persons.return_value = [{"id": "p1", "full_name": "Bob", "email": "bob@studio.dev"}]
            m_gazu.task.all_task_statuses.return_value = []
            with patch.object(content_sync, "_ayon_task_by_kitsu_id", return_value=ayon_task):
                with patch.object(
                    content_sync.ayon_api,
                    "get_activities",
                    side_effect=fake_get_activities,
                    create=True,
                ):
                    with patch.object(
                        content_sync.ayon_api,
                        "create_activity",
                        side_effect=fake_create_activity,
                        create=True,
                    ):
                        with patch.object(
                            content_sync.ayon_api,
                            "delete_activity",
                            side_effect=fake_delete_activity,
                            create=True,
                        ):
                            with patch.object(
                                content_sync,
                                "maybe_sync_checklist_subtasks_from_kitsu_comment",
                            ):
                                with patch.object(
                                    content_sync.ayon_api,
                                    "get_server_api_connection",
                                    return_value=con,
                                    create=True,
                                ):
                                    with patch.object(
                                        content_sync,
                                        "_resolve_ayon_login_for_comment_sync",
                                        return_value="bob.login",
                                    ):
                                        content_sync.sync_comment_to_ayon(
                                            processor, "kc1", "kt1", "pid",
                                        )
    # Upload pass + create pass each wrap mutations under as_username.
    assert con.as_username.call_count == 2
    assert all(c.args == ("bob.login",) for c in con.as_username.call_args_list)
    assert ctx.__enter__.call_count == 2
    assert "**[Bob]**" not in (stored[-1].get("body") or "")
    assert "delete_activity" in call_order
    assert "as_username_enter" in call_order
    assert "create_activity" in call_order
    enters = [i for i, x in enumerate(call_order) if x == "as_username_enter"]
    assert len(enters) == 2  # upload mutations + create mutations
    assert enters[0] < call_order.index("delete_activity") < enters[1]
    assert enters[1] < call_order.index("create_activity")


def test_ayon_task_by_kitsu_id_retries_once_on_502_then_succeeds():
    task = {"id": "t1", "data": {"kitsuId": "kid"}}
    resp502 = MagicMock()
    resp502.status_code = 502
    err502 = HTTPRequestError("502", response=resp502)
    seq = [err502, [task]]

    def fake_get_tasks(_pn):
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return iter(item)

    with patch.object(content_sync.time, "sleep") as m_sleep:
        with patch.object(content_sync.ayon_api, "get_tasks", side_effect=fake_get_tasks):
            r = content_sync._ayon_task_by_kitsu_id("P", "kid")
    assert r == task
    m_sleep.assert_called_once_with(content_sync._RETRY_SLEEP_SEC)


def test_ayon_lookup_cache_tasks_fetched_once(monkeypatch):
    calls = {"n": 0}

    def fake_get_tasks(pn):
        calls["n"] += 1
        assert pn == "ProjA"
        return [
            {"id": "a1", "data": {"kitsuId": "kitsu-a"}},
            {"id": "a2", "data": {"kitsuId": "kitsu-b"}},
        ]

    monkeypatch.setattr(content_sync.ayon_api, "get_tasks", fake_get_tasks)
    monkeypatch.setattr(content_sync.ayon_api, "get_folders", lambda _pn: [])
    with content_sync._content_sync_ayon_lookup_cache_scope("ProjA"):
        assert content_sync._ayon_task_by_kitsu_id("ProjA", "kitsu-a")["id"] == "a1"
        assert content_sync._ayon_task_by_kitsu_id("ProjA", "kitsu-b")["id"] == "a2"
        assert content_sync._ayon_task_by_kitsu_id("ProjA", "missing") is None
    assert calls["n"] == 1


def test_ayon_lookup_cache_folders_fetched_once(monkeypatch):
    calls = {"n": 0}

    def fake_get_folders(pn):
        calls["n"] += 1
        assert pn == "ProjB"
        return [{"id": "f1", "data": {"kitsuId": "entity-x"}}]

    monkeypatch.setattr(content_sync.ayon_api, "get_folders", fake_get_folders)
    monkeypatch.setattr(content_sync.ayon_api, "get_tasks", lambda _pn: [])
    with content_sync._content_sync_ayon_lookup_cache_scope("ProjB"):
        assert content_sync._ayon_folder_by_kitsu_id("ProjB", "entity-x")["id"] == "f1"
        assert content_sync._ayon_folder_by_kitsu_id("ProjB", "nope") is None
    assert calls["n"] == 1


def test_ayon_lookup_cache_vizdev_surrogate_single_get_tasks(monkeypatch):
    task_calls = {"n": 0}
    folder_calls = {"n": 0}
    concept_kitsu = "concept-uuid-1"
    surrogate = content_sync.concept_vizdev_surrogate_kitsu_id(concept_kitsu)

    def fake_get_folders(pn):
        folder_calls["n"] += 1
        return [{"id": "folder-ayon-id", "data": {"kitsuId": concept_kitsu}}]

    def fake_get_tasks(pn):
        task_calls["n"] += 1
        return [
            {
                "id": "vizdev-task",
                "folderId": "folder-ayon-id",
                "data": {"kitsuId": surrogate},
            },
        ]

    monkeypatch.setattr(content_sync.ayon_api, "get_folders", fake_get_folders)
    monkeypatch.setattr(content_sync.ayon_api, "get_tasks", fake_get_tasks)
    with content_sync._content_sync_ayon_lookup_cache_scope("ProjC"):
        r1 = content_sync._ayon_vizdev_task_by_surrogate(
            "ProjC", concept_kitsu, surrogate,
        )
        r2 = content_sync._ayon_vizdev_task_by_surrogate(
            "ProjC", concept_kitsu, surrogate,
        )
    assert r1 is not None and r1["id"] == "vizdev-task"
    assert r2 == r1
    assert folder_calls["n"] == 1
    assert task_calls["n"] == 1


def test_kitsu_png_thumbnail_relative_url():
    assert content_sync._kitsu_png_thumbnail_relative_url("pid-1") == (
        "pictures/thumbnails/preview-files/pid-1.png"
    )


def test_parse_kitsu_publish_comment_table_version_full_table():
    from processor import content_sync_browser_urls as bu

    text = """Note here

|  |  |
|--|--|
| version | `5` |
| family | `render` |
| name | `Main_ColorAnim` |
| task | `Layout ( Layout )` |
| uniqueSprites | `16` |
"""
    assert bu.parse_kitsu_publish_comment_table_version(text) == 5


def test_parse_kitsu_publish_comment_table_version_missing_optional_rows():
    from processor import content_sync_browser_urls as bu

    text = """|  |  |
|---|---|
| version | `2` |
| family | `review` |
| name | `ShotA` |
"""
    assert bu.parse_kitsu_publish_comment_table_version(text) == 2


def test_parse_kitsu_publish_comment_table_version_no_backticks():
    from processor import content_sync_browser_urls as bu

    text = "| version | 7 |\n"
    assert bu.parse_kitsu_publish_comment_table_version(text) == 7


def test_parse_kitsu_publish_comment_table_version_no_table():
    from processor import content_sync_browser_urls as bu

    assert bu.parse_kitsu_publish_comment_table_version("no version row") is None


def test_ayon_products_browser_url_with_encoded_uri():
    from processor import content_sync_browser_urls as bu

    uri = bu.ayon_entity_uri_product_version(
        "MyProject",
        "assets/Character",
        product_name="animationKitsuReview",
        version=3,
    )
    assert uri.startswith("ayon+entity://MyProject/")
    assert "product=animationKitsuReview" in uri
    assert "version=3" in uri
    href = bu.ayon_browser_url_products_with_uri(
        "https://studio.test",
        "MyProject",
        uri,
    )
    assert href is not None
    assert href.startswith("https://studio.test/projects/")
    assert "/products?" in href
    assert "uri=ayon" in href


def test_maybe_append_review_version_markdown_link_idempotent(monkeypatch):
    proc = MagicMock()
    proc.settings = {
        "sync_settings": {
            "content_sync": {
                "review_version_link_enabled": True,
                "web_ui_base_url": "https://studio.test",
            },
        },
    }
    comment = {
        "id": "c1",
        "text": "| version | `3` |\n",
        "previews": [],
    }
    monkeypatch.setattr(
        content_sync.gazu.task,
        "get_task",
        lambda _tid: {"task_type": {"name": "Animation"}},
    )
    monkeypatch.setattr(
        content_sync,
        "_ayon_task_by_kitsu_id",
        lambda _pn, _tid: {"id": "t1", "folderId": "f1"},
    )
    monkeypatch.setattr(
        content_sync.ayon_api,
        "get_folder_by_id",
        lambda _pn, _fid: {"path": "/assets/Hero"},
        raising=False,
    )
    monkeypatch.setattr(
        content_sync.ayon_api,
        "get_products",
        lambda *_a, **_k: [
            {"id": "p1", "name": "animationKitsuReview", "productType": "review"},
        ],
        raising=False,
    )
    monkeypatch.setattr(
        content_sync,
        "_find_review_version_id_for_revision",
        lambda *_a, **_k: "ver-uuid-1",
    )
    body = "hello\n"
    out = content_sync._maybe_append_review_version_markdown_link(
        proc, "Proj", "kitsu-task-1", comment, body,
    )
    assert "[Version 3](" in out
    assert "https://studio.test/projects/" in out
    assert "/products?" in out
    out2 = content_sync._maybe_append_review_version_markdown_link(
        proc, "Proj", "kitsu-task-1", comment, out,
    )
    assert out2 == out
