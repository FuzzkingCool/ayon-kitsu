# -*- coding: utf-8 -*-
"""Kitsu Socket.IO listener fast vs slow lane dispatch.

The Gazu event client invokes one listener at a time on the Socket.IO thread.
Structural handlers (task, shot, asset, …) stay on that thread but are timed.

Heavy handlers (comments, previews, playlists) are queued to a single worker
thread so the listener returns immediately and can process the next Socket.IO
message (e.g. task:update) while slow work runs asynchronously.

Ordering: slow jobs run strictly FIFO on one worker per processor instance.
Kitsu does not guarantee cross-entity ordering relative to the fast lane;
callers relying on ``comment:new`` after ``task:new`` for the same task should
treat that as best-effort (same as before when the queue was head-of-line
blocked).

Threading: the worker sets thread-local Kitsu host (see ``utils.set_kitsu_host``)
before each job when Gazu is installed. Gazu may share HTTP state across threads;
if you see sporadic Kitsu errors under load, set ``KITSU_PROCESSOR_GAZU_IO_LOCK=1``
to serialize all Gazu-bearing work (fast lane + slow worker).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from nxtools import log_traceback

_log = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _set_kitsu_host_for_worker(processor: Any) -> None:
    """Mirror main listener thread host setup when Gazu is available."""
    try:
        if importlib.util.find_spec("gazu") is None:
            return
    except (ImportError, ValueError, ModuleNotFoundError):
        # Tests may inject a minimal ``sys.modules['gazu']`` without a valid spec.
        return
    from . import utils as processor_utils

    processor_utils.set_kitsu_host(processor.kitsu_server_url)


class KitsuSocketLaneDispatcher:
    """Dispatch Socket.IO callbacks to fast (inline) vs slow (queued) execution."""

    __slots__ = (
        "_gazu_io_lock",
        "_log_timing_threshold_ms",
        "_processor",
        "_slow_q",
        "_started",
        "_worker",
    )

    def __init__(self, processor: Any) -> None:
        self._processor = processor
        max_q = _env_int("KITSU_PROCESSOR_SLOW_QUEUE_MAX", 10_000)
        self._slow_q: queue.Queue[tuple[str, Callable[[], None]]] = queue.Queue(
            maxsize=max(1, max_q)
        )
        self._log_timing_threshold_ms = _env_float(
            "KITSU_PROCESSOR_SOCKET_TIMING_MS", 100.0
        )
        self._started = False
        self._worker = threading.Thread(
            target=self._slow_worker_loop,
            name="KitsuSlowLane",
            daemon=True,
        )
        self._gazu_io_lock = threading.Lock() if _env_truthy(
            "KITSU_PROCESSOR_GAZU_IO_LOCK"
        ) else None

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._worker.start()
        _log.info("[kitsu_socket] slow lane worker started")

    def submit_slow(self, event_label: str, thunk: Callable[[], None]) -> None:
        """Queue ``thunk`` for the slow worker; non-blocking for the Socket.IO thread."""

        def _run() -> None:
            if self._gazu_io_lock:
                with self._gazu_io_lock:
                    thunk()
            else:
                thunk()

        try:
            self._slow_q.put_nowait((event_label, _run))
        except queue.Full:
            _log.error(
                "[kitsu_socket] slow lane queue full (max=%s); dropping %s",
                self._slow_q.maxsize,
                event_label,
            )

    def run_fast(self, event_label: str, thunk: Callable[[], None]) -> None:
        """Run a fast-lane handler with optional timing log and optional Gazu lock."""

        t0 = time.perf_counter()
        try:
            if self._gazu_io_lock:
                with self._gazu_io_lock:
                    thunk()
            else:
                thunk()
        except Exception:
            log_traceback(f"[kitsu_socket] fast_lane {event_label}")
            raise
        finally:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            if elapsed_ms >= self._log_timing_threshold_ms:
                _log.info(
                    "[kitsu_socket] fast_lane %s %.1fms (slow_queue_depth=%s)",
                    event_label,
                    elapsed_ms,
                    self.slow_queue_depth(),
                )
            else:
                _log.debug(
                    "[kitsu_socket] fast_lane %s %.1fms",
                    event_label,
                    elapsed_ms,
                )

    def slow_queue_depth(self) -> int:
        try:
            return self._slow_q.qsize()
        except NotImplementedError:
            return -1

    def _slow_worker_loop(self) -> None:
        while True:
            event_label, thunk = self._slow_q.get()
            _set_kitsu_host_for_worker(self._processor)
            t0 = time.perf_counter()
            try:
                thunk()
            except Exception:
                log_traceback(f"[kitsu_socket] slow_lane {event_label}")
            finally:
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                _log.info(
                    "[kitsu_socket] slow_lane done %s %.1fms (slow_queue_depth=%s)",
                    event_label,
                    elapsed_ms,
                    self.slow_queue_depth(),
                )


def add_listener_laned(
    processor: Any,
    event_client: Any,
    event_name: str,
    handler: Callable[[Any], None],
    *,
    lane: str,
) -> None:
    """Register a Gazu listener on ``lane`` ``\"fast\"`` or ``\"slow\"``."""

    import gazu

    disp: KitsuSocketLaneDispatcher | None = getattr(
        processor, "socket_lane_dispatcher", None
    )
    if disp is None:
        gazu.events.add_listener(event_client, event_name, handler)
        return

    if lane == "fast":

        def _wrapped_fast(data: Any, h: Callable[[Any], None] = handler) -> None:
            disp.run_fast(event_name, lambda: h(data))

        gazu.events.add_listener(event_client, event_name, _wrapped_fast)
        return

    if lane != "slow":
        raise ValueError(f"lane must be 'fast' or 'slow', got {lane!r}")

    def _wrapped_slow(data: Any, h: Callable[[Any], None] = handler) -> None:
        disp.submit_slow(event_name, lambda: h(data))

    gazu.events.add_listener(event_client, event_name, _wrapped_slow)
