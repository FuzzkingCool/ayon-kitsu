"""Custom logging handler that forwards logs to AYON server via events."""

import logging
import threading
import time
from datetime import datetime
from typing import Optional

import ayon_api


class ServerLogHandler(logging.Handler):
    """Logging handler that sends log records to AYON server as events.

    Logs are sent as events with topic 'addon.kitsu.processor.log'. The handler
    batches logs to avoid excessive API calls, sending them periodically or
    when the buffer reaches a certain size.
    """

    def __init__(
        self,
        sender: str,
        level: int = logging.NOTSET,
        batch_size: int = 10,
        flush_interval: float = 5.0,
    ):
        """Initialize the server log handler.

        Args:
            sender: Sender identifier for events (e.g., 'kitsu-processor-hostname')
            level: Minimum log level to forward (default: NOTSET, forwards all)
            batch_size: Number of logs to batch before sending (default: 10)
            flush_interval: Seconds between automatic flushes (default: 5.0)
        """
        super().__init__(level)
        self.sender = sender
        self.batch_size = batch_size
        self.flush_interval = flush_interval

        self._buffer: list[dict] = []
        self._buffer_lock = threading.Lock()
        self._last_flush = time.time()
        self._flush_thread: Optional[threading.Thread] = None
        self._stop_flush_thread = threading.Event()

        # Start background thread for periodic flushing
        if self.flush_interval > 0:
            self._flush_thread = threading.Thread(
                target=self._periodic_flush,
                daemon=True,
                name="ServerLogHandler-flush",
            )
            self._flush_thread.start()

    def emit(self, record: logging.LogRecord):
        """Handle a log record by adding it to the buffer."""
        try:
            # Format the log record
            log_data = {
                "level": record.levelname,
                "levelno": record.levelno,
                "message": self.format(record),
                "timestamp": datetime.fromtimestamp(
                    record.created
                ).isoformat(),
                "module": record.module,
                "funcName": record.funcName,
                "lineno": record.lineno,
                "pathname": record.pathname,
            }

            # Add exception info if present
            if record.exc_info:
                import traceback

                log_data["exc_info"] = traceback.format_exception(
                    *record.exc_info
                )

            # Add to buffer
            should_flush = False
            with self._buffer_lock:
                self._buffer.append(log_data)

                # Check if buffer is full (flush outside lock to avoid deadlock)
                if len(self._buffer) >= self.batch_size:
                    should_flush = True

            # Flush outside the lock to avoid deadlock
            if should_flush:
                self._flush_buffer()
        except Exception as e:
            # Avoid infinite recursion if logging fails
            # But try to log to stderr as last resort
            try:
                import sys

                print(
                    f"[ServerLogHandler] Error in emit: {e}", file=sys.stderr
                )
            except Exception:
                pass
            self.handleError(record)

    def _flush_buffer(self):
        """Send all buffered logs to the server."""
        if not self._buffer:
            return

        logs_to_send = []
        with self._buffer_lock:
            if self._buffer:
                logs_to_send = self._buffer[:]
                self._buffer.clear()
                self._last_flush = time.time()

        if not logs_to_send:
            return

        # Send logs as events
        try:
            # Check if service is initialized before trying to send
            try:
                # This will raise if service is not initialized
                ayon_api.get_service_addon_name()
            except (ValueError, AttributeError, Exception):
                # Service not ready yet, put logs back in buffer
                with self._buffer_lock:
                    self._buffer = logs_to_send + self._buffer
                return

            # Send each log as a separate event for better queryability
            for log_data in logs_to_send:
                try:
                    ayon_api.dispatch_event(
                        topic="addon.kitsu.processor.log",
                        sender=self.sender,
                        description=f"Processor log: {log_data['level']}",
                        summary={
                            "level": log_data["level"],
                            "levelno": log_data["levelno"],
                            "module": log_data["module"],
                            "funcName": log_data["funcName"],
                        },
                        payload=log_data,
                        finished=True,
                        store=True,
                    )
                except Exception as e:
                    # If dispatch fails, log to stderr to avoid losing the log
                    try:
                        import sys

                        print(
                            f"[ServerLogHandler] Failed to send log to server: {e}",
                            file=sys.stderr,
                        )
                    except Exception:
                        pass
        except Exception:
            # Silently fail to avoid recursion
            pass

    def _periodic_flush(self):
        """Background thread that periodically flushes the buffer."""
        while not self._stop_flush_thread.is_set():
            time.sleep(self.flush_interval)

            with self._buffer_lock:
                time_since_flush = time.time() - self._last_flush
                if time_since_flush >= self.flush_interval and self._buffer:
                    self._flush_buffer()

    def flush(self):
        """Flush all buffered logs immediately."""
        self._flush_buffer()
        super().flush()

    def close(self):
        """Close the handler and flush remaining logs."""
        self._stop_flush_thread.set()
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=2.0)
        self.flush()
        super().close()
