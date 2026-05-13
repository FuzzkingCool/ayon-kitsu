"""Unit tests for Kitsu Socket.IO fast vs slow lane dispatch."""

from __future__ import annotations

import logging
import sys
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

from processor.kitsu_socket_dispatch import (
    KitsuSocketLaneDispatcher,
    add_listener_laned,
)


class _FakeProcessor:
    kitsu_server_url = ""


def _install_fake_gazu(mock_add: MagicMock) -> types.ModuleType:
    fake = types.ModuleType("gazu")
    fake.events = types.SimpleNamespace(add_listener=mock_add)
    return fake


def test_slow_lane_executes_thunk():
    p = _FakeProcessor()
    d = KitsuSocketLaneDispatcher(p)
    d.start()
    done = threading.Event()
    seen: list[int] = []

    def thunk() -> None:
        seen.append(1)
        done.set()

    d.submit_slow("test:event", thunk)
    assert done.wait(timeout=3.0)
    assert seen == [1]


def test_run_fast_invokes_thunk():
    p = _FakeProcessor()
    d = KitsuSocketLaneDispatcher(p)
    d.start()
    acc: list[str] = []

    def thunk() -> None:
        acc.append("x")

    d.run_fast("asset:new", thunk)
    assert acc == ["x"]


def test_slow_queue_depth_non_negative():
    p = _FakeProcessor()
    d = KitsuSocketLaneDispatcher(p)
    d.start()
    depth = d.slow_queue_depth()
    assert isinstance(depth, int)
    assert depth >= 0


def test_add_listener_laned_no_dispatcher_passthrough(monkeypatch):
    mock_add = MagicMock()
    monkeypatch.setitem(sys.modules, "gazu", _install_fake_gazu(mock_add))
    proc = MagicMock(spec=["socket_lane_dispatcher"])
    proc.socket_lane_dispatcher = None
    ec = object()

    def handler(data):
        return data

    add_listener_laned(proc, ec, "task:update", handler, lane="fast")
    mock_add.assert_called_once_with(ec, "task:update", handler)


def test_add_listener_laned_slow_wraps(monkeypatch):
    mock_add = MagicMock()
    monkeypatch.setitem(sys.modules, "gazu", _install_fake_gazu(mock_add))
    proc = _FakeProcessor()
    disp = KitsuSocketLaneDispatcher(proc)
    disp.start()
    proc.socket_lane_dispatcher = disp
    ec = object()
    calls: list[dict] = []

    def handler(data) -> None:
        calls.append(dict(data))

    add_listener_laned(proc, ec, "comment:new", handler, lane="slow")
    mock_add.assert_called_once()
    wrapped = mock_add.call_args[0][2]
    wrapped({"id": 1})
    for _ in range(50):
        if calls:
            break
        time.sleep(0.05)
    assert calls == [{"id": 1}]


def test_slow_lane_drop_when_queue_full(monkeypatch, caplog):
    monkeypatch.setenv("KITSU_PROCESSOR_SLOW_QUEUE_MAX", "1")
    p = _FakeProcessor()
    d = KitsuSocketLaneDispatcher(p)
    d.start()
    gate = threading.Event()

    def block() -> None:
        gate.wait(timeout=2.0)

    def noop() -> None:
        pass

    d.submit_slow("block", block)
    time.sleep(0.05)
    with caplog.at_level(logging.ERROR, logger="processor.kitsu_socket_dispatch"):
        d.submit_slow("a", noop)
        time.sleep(0.05)
        d.submit_slow("b", noop)
    gate.set()
    time.sleep(0.25)
    assert "slow lane queue full" in caplog.text.lower()


def test_add_listener_laned_invalid_lane(monkeypatch):
    mock_add = MagicMock()
    monkeypatch.setitem(sys.modules, "gazu", _install_fake_gazu(mock_add))
    proc = _FakeProcessor()
    proc.socket_lane_dispatcher = KitsuSocketLaneDispatcher(proc)
    with pytest.raises(ValueError, match="lane"):
        add_listener_laned(
            proc,
            object(),
            "x",
            lambda d: None,
            lane="invalid",
        )
