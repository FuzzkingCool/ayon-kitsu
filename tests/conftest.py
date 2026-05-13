"""Pytest plugins: minimal ``ayon_api`` tree so processor imports work without the real SDK."""

import sys
import types

import pytest


def _install_minimal_ayon_api() -> None:
    """Always replace ``ayon_api`` with a stub (unit tests do not talk to AYON)."""
    for key in list(sys.modules):
        if key == "ayon_api" or key.startswith("ayon_api."):
            del sys.modules[key]

    exc = types.ModuleType("ayon_api.exceptions")

    class HTTPRequestError(Exception):
        def __init__(self, message="", *args, response=None, **kwargs):
            super().__init__(message)
            self.response = response

    class ServerError(Exception):
        pass

    exc.HTTPRequestError = HTTPRequestError
    exc.ServerError = ServerError

    ayon = types.ModuleType("ayon_api")
    ayon.exceptions = exc
    ayon.post = lambda *args, **kwargs: None
    ayon.get_folders = lambda *args, **kwargs: []
    ayon.get_tasks = lambda *args, **kwargs: []
    ayon.get_folder_by_id = lambda *args, **kwargs: None
    ayon.get_tasks_by_folder_path = lambda *args, **kwargs: []
    ayon.update_folder = lambda *args, **kwargs: None
    ayon.update_task = lambda *args, **kwargs: None
    ayon.send_batch_operations = lambda *args, **kwargs: None
    ayon.create_event = lambda **kwargs: "stub-event-id"
    ayon.update_event = lambda *args, **kwargs: None
    ayon.get_base_url = lambda: "https://ayon.test"

    ayon_utils = types.ModuleType("ayon_api.utils")
    ayon_utils.create_entity_id = lambda: "00000000-0000-0000-0000-000000000001"

    sys.modules["ayon_api"] = ayon
    sys.modules["ayon_api.exceptions"] = exc
    sys.modules["ayon_api.utils"] = ayon_utils


_install_minimal_ayon_api()


@pytest.fixture(autouse=True)
def _clear_processor_content_sync_ayon_lookup_pass() -> None:
    """``content_sync`` uses a module-global pass cache; tests must not leak it across cases."""
    try:
        from processor import content_sync

        content_sync._CONTENT_SYNC_AYON_LOOKUP_PASS = None
    except Exception:
        pass
    yield
    try:
        from processor import content_sync

        content_sync._CONTENT_SYNC_AYON_LOOKUP_PASS = None
    except Exception:
        pass
