"""Every change to the vault goes through here: one queue, files then index, then one change-log line."""

from __future__ import annotations

import re
import threading
from datetime import date, datetime

from .changelog import HUMAN, ChangeLog
from .index import Index
from .notes import (
    LINK,
    STATUSES,
    SYSTEM_LINK_KEYS,
    add_history,
    field_links,
    format_value,
    history_span,
    parse,
    relation_edges,
    set_fields,
    sha256,
)
from .rerank import Reranker
from .search import nearest_live_note
from .secrets_check import find_secret
from .vault import ARCHIVE, Vault, VaultError

WRITABLE = ("memory", "entities", "docs")
LOG_FOLDERS = {"daily": "daily", "debrief": "debriefs", "raw": "raw"}
# Too ordinary to mean anything when they turn up elsewhere in the vault.
COMMON_VALUES = {"true", "false", "yes", "no", "none", "null", "current", "superseded", "active", "done"}


def build_context(vault: Vault) -> str:
    """core/ plus the memory index, cut to the token budget (about 4 characters a token)."""
    budget = vault.settings.context_tokens * 4
    sections = []
    for path in vault.all_paths(["core"]):
        sections.append(f"<!-- {path} -->\n{vault.read_text(path).strip()}")
    core = "\n\n".join(sections)
    if len(core) > budget:
        core = core[:budget] + "\n[core truncated at the token budget]"
    lines, omitted = [], 0
    used = len(core) + 30
    memory = vault.all_paths(["memory"])
    memory.sort(key=lambda p: (vault.root / p).stat().st_mtime, reverse=True)
    for path in memory:
        note = vault.load(path)
        if note.status != "current":
            continue
        line = f"- {path[len('memory/'):-3]}: {note.body.strip()}"
        if used + len(line) + 1 > budget:
            omitted += 1
            continue
        lines.append(line)
        used += len(line) + 1
    index = "\n".join(lines) or "(no memories yet)"
    if omitted:
        index += f"\n[{omitted} older memories over the budget; use search]"
    return f"{core}\n\n# Memory index\n{index}"


class Store:
    def __init__(self, vault: Vault, index: Index):
        self.vault = vault
        self.index = index
        self.changes = ChangeLog(vault.settings.state_dir / "changes.jsonl")
        self.lock = threading.RLock()
        self.on_change = lambda: None
        # Agent searches rerank through this when the vault turns it on; None keeps RRF order.
        self.reranker: Reranker | None = None

    # Reads

    def read(self, path: str) -> dict:
        if path == "@context":
            return {"path": "@context", "content": build_context(self.vault)}
        clean = self.vault.clean(path, must_exist=True, archive_ok=True)
        text = self.vault.read_text(clean)
        return {
            "path": clean,
            "content": text,
            "hash": sha256(text),
            "links": self.index.links(clean),
            "backlinks": self.index.backlinks(clean),
            "history": parse(text).history,
        }

    def list(self, folder: str = "") -> dict:
        clean = self.vault.clean_folder(folder)
        paths = self.vault.all_paths([clean.split("/")[0]] if clean else None)
        if clean:
            paths = [p for p in paths if p.startswith(clean + "/")]
        paths.sort(key=lambda p: (self.vault.root / p).stat().st_mtime, reverse=True)
        return {"folder": clean or "/", "files": paths}

    # Writes

    def write(self, path: str, content: str, expected_hash: str | None = None, *, actor: str, human: bool = False) -> dict:
        clean = self.vault.clean(path)
        self._check_writable(clean, human)
        self._check_secret(content)
        with self.lock:
            exists = (self.vault.root / clean).is_file()
            if expected_hash is None:
                if exists:
                    raise VaultError(f"{clean} already exists. read it, then pass its hash to replace it, or use update.")
                content = self._prepare_new(clean, content, human)
            else:
                if not exists:
                    raise VaultError(f"no such note: {clean}")
                self._check_hash(clean, expected_hash)
            self._check_shape(clean, content, human)
            self._check_limits(clean, content, self.vault.read_text(clean) if exists else None)
            return self._save({clean: content}, "write", actor, clean)

    def update(self, path: str, fields: dict, reason: str, expected_hash: str, *, actor: str) -> dict:
        clean = self.vault.clean(path, must_exist=True)
        self._check_writable(clean)
        self._check_secret(reason + " " + " ".join(format_value(v) for v in fields.values()))
        if not fields or not reason.strip():
            raise VaultError("update needs at least one field and a reason")
        if set(fields) & SYSTEM_LINK_KEYS:
            raise VaultError("superseded_by is set by supersede; use that to replace a note")
        with self.lock:
            self._check_hash(clean, expected_hash)
            text = self.vault.read_text(clean)
            current = parse(text).fields
            changed = {k: v for k, v in fields.items() if _plain(current.get(k)) != _plain(v)}
            if not changed:
                return {"path": clean, "hash": sha256(text), "stale_copies": []}
            new_text = set_fields(text, changed)
            for key, value in changed.items():
                old = _plain(current.get(key)) or "(unset)"
                new_text = add_history(new_text, f"{date.today()}: {key} {old} -> {_plain(value)} ({reason.strip()})")
            self._check_relations(new_text)
            self._check_limits(clean, new_text, text)
            result = self._save({clean: new_text}, "update", actor, clean)
            result["stale_copies"] = self._stale_copies(clean, [current.get(k) for k in changed])
            return result

    def supersede(self, old: str, new: str, reason: str, *, actor: str) -> dict:
        """`new` is an existing note's path, or the replacement's text to create beside the old note."""
        old_path = self.vault.clean(old, must_exist=True)
        self._check_writable(old_path)
        self._check_secret(reason)
        with self.lock:
            old_text = self.vault.read_text(old_path)
            if parse(old_text).status != "current":
                raise VaultError(f"{old_path} is already {parse(old_text).status}")
            changes: dict[str, str] = {}
            before: dict[str, str | None] = {old_path: old_text}
            if re.fullmatch(r"[\w./ -]+\.md", new.strip()):
                new_path = self.vault.clean(new.strip(), must_exist=True)
                self._check_writable(new_path)
                new_text = self.vault.read_text(new_path)
                before[new_path] = new_text
            else:
                self._check_secret(new)
                new_path = self._free_slug(old_path, new)
                new_text = _with_status(new.strip() + "\n")
                self._check_shape(new_path, new_text, False)
                before[new_path] = None
            if new_path == old_path:
                raise VaultError("a note cannot supersede itself")
            today = str(date.today())
            old_link, new_link = f"[[{old_path[:-3]}]]", f"[[{new_path[:-3]}]]"
            old_text = set_fields(old_text, {"status": "superseded", "superseded_by": new_link, "valid_until": today})
            changes[old_path] = add_history(old_text, f"{today}: superseded by {new_link} ({reason.strip()})")
            changes[new_path] = set_fields(new_text, {"replaces": old_link})
            for path, text in changes.items():
                self._check_limits(path, text, before[path])
            return self._save(changes, "supersede", actor, new_path)

    def move(self, path: str, to: str, *, actor: str) -> dict:
        source = self.vault.clean(path, must_exist=True)
        target = self.vault.clean(to, archive_ok=True)
        # Logs are append-only, but old ones must leave logs/ for .archive/ (dreaming, SPEC §5).
        if not (source.startswith("logs/") and target.startswith(ARCHIVE + "/")):
            self._check_writable(source)
        if not target.startswith(ARCHIVE + "/"):
            self._check_writable(target)
        with self.lock:
            if (self.vault.root / target).exists():
                raise VaultError(f"{target} already exists")
            changes = {target: self.vault.read_text(source)}
            for other in self.vault.all_paths(["memory", "entities", "docs", "core"]):
                base = changes[target] if other == source else self.vault.read_text(other)
                rewritten = _rewrite_links(base, source, target, self.vault)
                if other == source:
                    changes[target] = rewritten
                elif rewritten != base:
                    changes[other] = rewritten
            (self.vault.root / source).unlink()
            self.index.remove_file(source, relink=False)
            linked_from = sorted(path for path in changes if path != target)
            result = self._save(changes, "move", actor, target, removed=[source])
            return {**result, "linked_from": linked_from}

    def save_log(self, kind: str, text: str, day: str | None = None, *, actor: str) -> dict:
        if kind not in LOG_FOLDERS:
            raise VaultError(f"kind must be one of {sorted(LOG_FOLDERS)}")
        self._check_secret(text)
        now = datetime.now()
        today = f"{now:%Y-%m-%d}"
        day = _log_day(day) if day else today
        path = f"logs/{LOG_FOLDERS[kind]}/{day}.md"
        # An entry added to another day says when it was really written.
        heading = f"{now:%H:%M:%S}" if day == today else f"added {today} {now:%H:%M:%S}"
        with self.lock:
            full = self.vault.root / path
            existing = full.read_text() if full.exists() else f"# {kind} {day}\n"
            entry = f"{existing.rstrip()}\n\n## {heading} ({actor})\n{text.strip()}\n"
            return self._save({path: entry}, "save_log", actor, path)

    def sync_outside_edits(self) -> list[str]:
        """Pick up edits made outside Cortex: reindex them and log them as a human edit.

        The index is the baseline: a file whose hash differs from its indexed one changed outside.
        An empty index (first start, or index.sqlite lost) has no baseline, so nothing is logged.
        """
        with self.lock:
            fresh = self.index.is_empty()
            changed = self.index.sync(backfill=False)
            if changed and not fresh:
                self.changes.append(HUMAN, "edit", changed)
            if changed:
                self.on_change()
        # Outside the store lock: a slow embedder must not hold up writes.
        self.index.backfill()
        return changed

    # Internals

    def _save(self, changes: dict[str, str], action: str, actor: str, primary: str, removed: list[str] | None = None) -> dict:
        # A save that changes nothing leaves no trace, so "no entries" really means no change.
        changes = {p: t for p, t in changes.items() if not self._same_on_disk(p, t)}
        removed = removed or []
        for path, text in changes.items():
            self.vault.write_text(path, text)
        for path in changes:
            self.index.index_file(path, relink=False)
        if changes or removed:
            self.index.relink()
            self.changes.append(actor, action, removed + list(changes))
            self.on_change()
        return {"path": primary, "hash": self.vault.hash(primary)}

    def _same_on_disk(self, path: str, text: str) -> bool:
        full = self.vault.root / path
        return full.is_file() and full.read_text(encoding="utf-8") == text

    def _check_writable(self, path: str, human: bool = False) -> None:
        folder = path.split("/")[0]
        if folder == "core" and not human:
            raise VaultError("core/ is edited only by you in the /ui editor; agents cannot write it")
        if folder == "logs":
            raise VaultError("logs/ is append-only; use save_log")
        if folder not in WRITABLE and folder != "core":
            raise VaultError(f"cannot write to {folder}/")

    def _check_secret(self, text: str) -> None:
        found = find_secret(text)
        if found:
            raise VaultError(f"refused: text looks like it contains a secret ({found}). Remove it and retry.")

    def _check_hash(self, path: str, expected: str) -> None:
        actual = self.vault.hash(path)
        if expected != actual:
            raise VaultError(f"stale hash for {path}: the note changed. read it again, then retry with hash {actual}.")

    def _check_shape(self, path: str, content: str, human: bool) -> None:
        if path.startswith("memory/") and not human:
            body = parse(content).body.strip()
            if not body or "\n" in body:
                raise VaultError("a memory line is one sentence on one line; longer detail belongs in a card or doc it links to")
        self._check_relations(content)

    def _check_limits(self, path: str, content: str, before: str | None) -> None:
        """Refuse a note that breaks a vault rule, unless it was already broken and got no worse.

        Notes written before a rule existed must stay editable, so a fix can land in steps.
        """
        new = parse(content)
        old = parse(before) if before is not None else None
        status = new.declared_status
        if status not in STATUSES and (old is None or old.declared_status != status):
            raise VaultError(
                f"status {status!r} is not allowed; use one of {', '.join(STATUSES)}. "
                "Put versions or phases in a field of their own, such as phase:"
            )
        limit = self._body_limit(path)
        size = len(new.body_without_history.strip())
        if limit is not None and size > limit and (old is None or size > len(old.body_without_history.strip())):
            raise VaultError(
                f"{path} has {size} characters of body; {path.split('/')[0]}/ allows {limit} characters. "
                "move detail to the repo; link it"
            )

    def _body_limit(self, path: str) -> int | None:
        """Only memory lines and cards have a size limit; docs/, core/ and logs/ hold longer text."""
        settings = self.vault.settings
        return {"memory": settings.memory_body_limit, "entities": settings.entity_body_limit}.get(path.split("/")[0])

    def _check_relations(self, content: str) -> None:
        allowed = self.vault.relation_types()
        if not allowed:
            return
        unknown = {key for key, _ in relation_edges(parse(content))} - allowed - SYSTEM_LINK_KEYS
        if unknown:
            raise VaultError(f"unknown relation types {sorted(unknown)}. Allowed (core/relations.md): {sorted(allowed)}")

    def _prepare_new(self, path: str, content: str, human: bool) -> str:
        folder = path.split("/")[0]
        if folder in ("memory", "entities") and not content.startswith("---\n"):
            content = _with_status(content)
        if not human:
            duplicate = self._live_duplicate(path, content, folder)
            if duplicate:
                raise VaultError(
                    f"{duplicate} already covers this subject. read it and use update or supersede "
                    f"instead of a second note. Existing note:\n{self.vault.read_text(duplicate)}"
                )
        return content

    def _live_duplicate(self, path: str, content: str, folder: str) -> str | None:
        stem = path.rsplit("/", 1)[-1].lower()
        title = parse(content).title.lower()
        rows = self.index.db.execute(
            "SELECT path, title FROM files WHERE status='current' AND folder IN ('memory','entities','docs') "
            "AND path NOT IN (SELECT path FROM expiry WHERE expires < date('now','localtime'))"
        ).fetchall()
        for row in rows:
            same_name = row["path"].rsplit("/", 1)[-1].lower() == stem and row["path"].split("/")[0] != "docs"
            if same_name or (title and row["title"].lower() == title):
                return row["path"]
        body = parse(content).body.strip()
        # Cards are identified by name; similar wording doesn't make two things the same thing.
        if folder != "memory" or not body:
            return None
        return nearest_live_note(self.index, f"{path}\n{title}\n{body}", folder, self.vault.settings.duplicate_threshold)

    def _free_slug(self, beside: str, text: str) -> str:
        folder = beside.rsplit("/", 1)[0]
        words = re.findall(r"[a-z0-9]+", LINK.sub(r"\1", text).lower())[:6]
        slug = "-".join(words) or "note"
        candidate, n = f"{folder}/{slug}.md", 2
        while (self.vault.root / candidate).exists():
            candidate, n = f"{folder}/{slug}-{n}.md", n + 1
        return candidate

    def _stale_copies(self, path: str, old_values: list) -> list[dict]:
        """Where the old value still appears, leaving out History, logs and everyday words."""
        needles = [_plain(v) for v in old_values]
        needles = [v for v in needles if v and len(v) >= 3 and v.lower() not in COMMON_VALUES]
        stale = []
        for other in self.vault.all_paths(["core", "memory", "entities", "docs"]):
            if other == path:
                continue
            text = self.vault.read_text(other)
            start, end = history_span(text)
            if start is not None:
                text = text[:start] + text[end:]
            for needle in needles:
                for line in text.splitlines():
                    if re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", line):
                        stale.append({"path": other, "value": needle, "line": line.strip()[:200]})
        return stale


def _plain(value) -> str:
    """A field value as words, so old and new compare the same whichever way YAML read them."""
    if value is None:
        return ""
    links = field_links(value)
    if links:
        return ", ".join(f"[[{link}]]" for link in links)
    return str(value)


def _log_day(day: str) -> str:
    try:
        return datetime.strptime(day, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise VaultError(f"date must be YYYY-MM-DD, got {day!r}") from None


def _with_status(content: str) -> str:
    return f"---\nstatus: current\n---\n{content}"


def _rewrite_links(text: str, source: str, target: str, vault: Vault) -> str:
    source_stem, source_path = source.rsplit("/", 1)[-1][:-3].lower(), source[:-3].lower()

    def replace(match: re.Match) -> str:
        name = match.group(1).strip().removesuffix(".md").lower()
        if name != source_path and name != source_stem:
            return match.group(0)
        if name == source_stem and vault.resolve_link(name) != source:
            return match.group(0)
        tail = match.group(0)[2 + len(match.group(1)) :]
        return f"[[{target[:-3]}{tail}"

    return LINK.sub(replace, text)
