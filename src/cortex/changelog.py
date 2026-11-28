"""The vault's change log: one JSON line per change, in <vault>/.cortex/changes.jsonl.

The vault is plain files (backups cover it), so this is the only record of who changed what
and when. The dreaming job reads it to find its own changes and to skip a run when nothing
changed, so the line format is a contract:

    {"at": "2026-11-27T10:00:00Z", "actor": "laptop", "action": "write", "paths": ["memory/x.md"]}

`at` is UTC to the second with a trailing Z, so lines also sort as plain strings. `action` is
one of ACTIONS; `edit` is a change the file watcher saw that didn't come through Cortex, made
by HUMAN. `paths` names every file written or removed, vault-relative.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

HUMAN = "human"
ACTIONS = ("write", "update", "supersede", "move", "save_log", "edit")


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _moment(stamp: str) -> datetime:
    """Either Z or +00:00 reads the same, so callers needn't match the writer's spelling."""
    moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


class ChangeLog:
    def __init__(self, file: Path):
        self.file = file

    def append(self, actor: str, action: str, paths: list[str]) -> dict:
        """Callers hold the store lock, so lines never interleave."""
        if action not in ACTIONS:
            raise ValueError(f"unknown change action: {action}")
        entry = {"at": now(), "actor": actor, "action": action, "paths": list(paths)}
        self.file.parent.mkdir(parents=True, exist_ok=True)
        with self.file.open("a", encoding="utf-8") as out:
            out.write(json.dumps(entry) + "\n")
        return entry

    def entries(self, since: str | None = None) -> list[dict]:
        """Every entry, oldest first; with `since` (ISO-8601), only those strictly after it."""
        if not self.file.is_file():
            return []
        found = [json.loads(line) for line in self.file.read_text(encoding="utf-8").splitlines() if line.strip()]
        if since is None:
            return found
        after = _moment(since)
        return [entry for entry in found if _moment(entry["at"]) > after]

    def for_path(self, path: str) -> list[dict]:
        """Entries that touched `path`, newest first: the note's history in the UI."""
        return [entry for entry in reversed(self.entries()) if path in entry["paths"]]

    def count(self) -> int:
        if not self.file.is_file():
            return 0
        with self.file.open(encoding="utf-8") as lines:
            return sum(1 for line in lines if line.strip())
