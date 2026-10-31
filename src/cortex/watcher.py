"""Background upkeep: commit outside edits after a quiet period, push on a timer."""

from __future__ import annotations

import logging
import threading

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .store import Store

log = logging.getLogger("cortex.watcher")


class _Handler(FileSystemEventHandler):
    def __init__(self, changed: threading.Event, root: str):
        self.changed = changed
        self.root = root

    def on_any_event(self, event) -> None:
        relative = str(event.src_path).removeprefix(self.root).lstrip("/")
        if event.event_type in ("opened", "closed_no_write"):
            return
        if not relative.startswith((".git", ".cortex")):
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
        threading.Thread(target=self._push_loop, daemon=True, name="cortex-push").start()

    def stop(self) -> None:
        self.stop_event.set()
        self.changed.set()
        self.observer.stop()

    def _watch_loop(self) -> None:
        while not self.stop_event.is_set():
            self.changed.wait()
            # Wait for the folder to go quiet before treating the edit as finished.
            while self.changed.is_set() and not self.stop_event.is_set():
                self.changed.clear()
                self.stop_event.wait(self.settings.quiet_seconds)
            if self.stop_event.is_set():
                return
            try:
                dirty = self.store.sync_outside_edits()
                if dirty:
                    log.info("committed outside edits: %s", dirty)
            except Exception:
                log.exception("could not sync outside edits")

    def _push_loop(self) -> None:
        while not self.stop_event.wait(self.settings.push_minutes * 60):
            for failure in self.store.git.push():
                log.warning("push failed: %s", failure)
