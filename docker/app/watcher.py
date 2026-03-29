"""
watcher.py — Debounced filesystem watcher for retroshare.

Watches a list of source directories for any filesystem changes and invokes
a callback after a quiet period (DEBOUNCE_SECONDS) with no new events. This
prevents thrashing during bulk file copies.

Typical usage::

    watcher = SourceWatcher()
    watcher.start(paths=["/sources/roms1", "/sources/roms2"], on_change_callback=rebuild)
    # ... later, when sources change via the API:
    watcher.update_paths(["/sources/roms1", "/sources/roms3"])
    # ... on shutdown:
    watcher.stop()
"""

import logging
import os
import threading

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEBOUNCE_SECONDS = 30

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal event handler
# ---------------------------------------------------------------------------


class _DebounceHandler(FileSystemEventHandler):
    """Watchdog event handler that debounces all events into a single callback.

    Every filesystem event resets a timer. The callback is only invoked after
    DEBOUNCE_SECONDS of silence (no new events).
    """

    def __init__(self, on_change, debounce_seconds=DEBOUNCE_SECONDS):
        super().__init__()
        self._on_change = on_change
        self._debounce_seconds = debounce_seconds
        self._lock = threading.Lock()
        self._timer = None

    def on_any_event(self, event):
        logger.debug("FS event: %s %s", event.event_type, event.src_path)
        self._reset_timer()

    def _reset_timer(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_seconds, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        logger.info("Debounce period elapsed — triggering rebuild callback")
        self._on_change()

    def cancel(self):
        """Cancel any pending debounce timer."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class SourceWatcher:
    """Watches a set of source directories and calls a callback on changes.

    Thread-safe: the callback will be invoked from a background timer thread.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._observer = None
        self._handler = None
        self._callback = None

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def start(self, paths, on_change_callback):
        """Begin watching *paths* and call *on_change_callback* on changes.

        Args:
            paths: iterable of directory path strings to watch.
            on_change_callback: callable invoked (with no arguments) after
                DEBOUNCE_SECONDS of quiet following any filesystem event.
        """
        paths = list(paths)
        with self._lock:
            self._callback = on_change_callback
            self._handler = _DebounceHandler(on_change_callback)
            self._observer = self._build_observer(paths, self._handler)
            self._observer.start()
            logger.info("SourceWatcher started, watching %d path(s)", len(paths))

    def update_paths(self, paths):
        """Stop watching old paths and start watching *paths* instead.

        The existing callback is preserved. Safe to call at any time.

        Args:
            paths: iterable of directory path strings to watch.
        """
        paths = list(paths)
        with self._lock:
            if self._callback is None:
                logger.warning(
                    "update_paths called before start(); ignoring"
                )
                return
            self._teardown_locked()
            self._handler = _DebounceHandler(self._callback)
            self._observer = self._build_observer(paths, self._handler)
            self._observer.start()
            logger.info(
                "SourceWatcher paths updated, now watching %d path(s)",
                len(paths),
            )

    def stop(self):
        """Tear down all watchers and cancel any pending debounce timer."""
        with self._lock:
            self._teardown_locked()
        logger.info("SourceWatcher stopped")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _teardown_locked(self):
        """Stop observer and cancel timer. Must be called with self._lock held."""
        if self._handler is not None:
            self._handler.cancel()
            self._handler = None

        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error stopping observer: %s", exc)
            self._observer = None

    @staticmethod
    def _build_observer(paths, handler):
        """Create and schedule an Observer for the given paths.

        Paths that do not exist are skipped with a warning rather than
        raising an exception.

        Args:
            paths: iterable of directory path strings.
            handler: a FileSystemEventHandler instance.

        Returns:
            A configured (but not yet started) Observer.
        """
        observer = Observer()
        scheduled = 0
        for path in paths:
            if not os.path.isdir(path):
                logger.warning(
                    "Skipping watch path (not a directory or does not exist): %s",
                    path,
                )
                continue
            observer.schedule(handler, path, recursive=True)
            logger.info("Scheduled watch on: %s", path)
            scheduled += 1

        if scheduled == 0:
            logger.warning("No valid paths to watch; observer will be idle")

        return observer
