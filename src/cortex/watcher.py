"""Background upkeep: after the folder goes quiet, reindex outside edits and log them."""

from __future__ import annotations

import logging
import threading

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .store import Store

log = logging.getLogger("cortex.watcher")

BACKFILL_SECONDS = 60


class _Handler(FileSystemEventHandler):
    def __init__(self, changed: threading.Event, root: str):
        self.changed = changed
        self.root = root

    def on_any_event(self, event) -> None:
        relative = str(event.src_path).removeprefix(self.root).lstrip("/")
        if event.event_type in ("opened", "closed_no_write"):
            return
        # A .git left over from older versions is ignored, like Cortex's own state.
        if relative.split("/")[0] not in (".git", ".cortex"):
            self.changed.set()


class Background:
    def __init__(self, store: Store):
        self.store = store
        self.settings = store.vault.settings
        self.stop_event = threading.Event()
        self.changed = threading.Event()
        self.observer = Observer()

    def start(self) -> None:
        root = str(self.settings.vault)
        self.observer.schedule(_Handler(self.changed, root), root, recursive=True)
        self.observer.start()
        threading.Thread(target=self._watch_loop, daemon=True, name="cortex-watch").start()

    def stop(self) -> None:
        self.stop_event.set()
        self.changed.set()
        self.observer.stop()

    def _watch_loop(self) -> None:
        while not self.stop_event.is_set():
            if not self.changed.wait(BACKFILL_SECONDS):
                # Quiet folder: a good moment to retry vectors the embedder failed to give earlier.
                if self.store.index.pending_count():
                    added = self.store.index.backfill()
                    if added:
                        log.info("backfilled %d vectors", added)
                continue
            # Wait for the folder to go quiet before treating the edit as finished.
            while self.changed.is_set() and not self.stop_event.is_set():
                self.changed.clear()
                self.stop_event.wait(self.settings.quiet_seconds)
            if self.stop_event.is_set():
                return
            try:
                changed = self.store.sync_outside_edits()
                if changed:
                    log.info("logged outside edits: %s", changed)
            except Exception:
                log.exception("could not sync outside edits")
