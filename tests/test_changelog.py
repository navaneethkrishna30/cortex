import json
import re

from cortex.changelog import HUMAN, ChangeLog
from cortex.config import Settings
from cortex.server import open_store

CARD = "---\nstatus: current\nport: 8080\n---\n# App\nThe app.\n"


def logged(store):
    return store.changes.entries()


def last(store):
    return logged(store)[-1]


# The file format is a contract with the dreaming job, so it is checked byte for byte.


def test_entry_is_one_json_line_in_the_agreed_shape(tmp_path):
    log = ChangeLog(tmp_path / ".cortex/changes.jsonl")
    log.append("laptop", "write", ["memory/x.md"])
    line = (tmp_path / ".cortex/changes.jsonl").read_text()
    assert line.endswith("\n") and line.count("\n") == 1
    entry = json.loads(line)
    assert list(entry) == ["at", "actor", "action", "paths"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", entry["at"])
    assert entry | {"at": None} == {"at": None, "actor": "laptop", "action": "write", "paths": ["memory/x.md"]}


def test_entries_since_keeps_only_later_ones(tmp_path):
    file = tmp_path / "changes.jsonl"
    rows = [
        {"at": "2026-11-01T10:00:00Z", "actor": "a", "action": "write", "paths": ["memory/a.md"]},
        {"at": "2026-11-02T10:00:00Z", "actor": "dreamer", "action": "update", "paths": ["memory/b.md"]},
    ]
    file.write_text("".join(json.dumps(r) + "\n" for r in rows))
    log = ChangeLog(file)
    assert log.entries() == rows
    assert log.entries("2026-11-01T10:00:00Z") == rows[1:]
    assert log.entries("2026-11-02T10:00:00+00:00") == []
    assert log.count() == 2


def test_missing_log_reads_as_empty(tmp_path):
    log = ChangeLog(tmp_path / "nothing.jsonl")
    assert log.entries() == [] and log.count() == 0


# One entry per change, named for the caller


def test_write_and_replace_are_logged_as_write(store):
    made = store.write("entities/app.md", CARD, actor="laptop")
    assert last(store) | {"at": None} == {"at": None, "actor": "laptop", "action": "write", "paths": ["entities/app.md"]}
    store.write("entities/app.md", CARD.replace("8080", "9090"), made["hash"], actor="phone")
    assert (last(store)["actor"], last(store)["action"]) == ("phone", "write")
    assert len(logged(store)) == 2


def test_identical_rewrite_and_unchanged_update_log_nothing(store):
    made = store.write("entities/app.md", CARD, actor="a")
    store.write("entities/app.md", CARD, made["hash"], actor="a")
    store.update("entities/app.md", {"port": 8080}, "same", made["hash"], actor="a")
    assert len(logged(store)) == 1


def test_update_supersede_move_and_log_name_every_path(store):
    made = store.write("entities/app.md", CARD, actor="a")
    store.update("entities/app.md", {"port": 9090}, "moved", made["hash"], actor="b")
    assert (last(store)["action"], last(store)["paths"]) == ("update", ["entities/app.md"])
    store.write("memory/focus.md", "Current focus is gardening.", actor="a")
    new = store.supersede("memory/focus.md", "Current focus is beekeeping.", "changed", actor="dreamer")["path"]
    assert last(store) | {"at": None} == {"at": None, "actor": "dreamer", "action": "supersede", "paths": ["memory/focus.md", new]}
    store.write("docs/a.md", "# A\nSee [[app]].\n", actor="a")
    store.move("entities/app.md", "entities/service.md", actor="c")
    assert (last(store)["action"], last(store)["paths"]) == ("move", ["entities/app.md", "entities/service.md", "docs/a.md"])
    path = store.save_log("raw", "Something happened.", actor="d")["path"]
    assert last(store) | {"at": None} == {"at": None, "actor": "d", "action": "save_log", "paths": [path]}


def test_outside_edit_logged_as_human_edit(store):
    (store.vault.root / "docs").mkdir()
    (store.vault.root / "docs/hand.md").write_text("# Hand\nEdited over SSH\n")
    assert store.sync_outside_edits() == ["docs/hand.md"]
    assert last(store) | {"at": None} == {"at": None, "actor": HUMAN, "action": "edit", "paths": ["docs/hand.md"]}
    assert store.sync_outside_edits() == []
    assert len(logged(store)) == 1


def test_outside_delete_is_logged_too(store):
    store.write("docs/gone.md", "# Gone\nSoon.\n", actor="a")
    (store.vault.root / "docs/gone.md").unlink()
    assert store.sync_outside_edits() == ["docs/gone.md"]
    assert (last(store)["action"], last(store)["paths"]) == ("edit", ["docs/gone.md"])


def test_store_writes_are_not_logged_again_as_outside_edits(store):
    store.write("docs/a.md", "# A\nText.\n", actor="a")
    assert store.sync_outside_edits() == []
    assert [e["action"] for e in logged(store)] == ["write"]


# Startup: no git, and a leftover .git is left alone


def hash_settings(settings):
    return Settings(vault=settings.vault, auth_db=settings.auth_db, embedding_provider="hash")


def test_startup_creates_no_git_repo(settings):
    store = open_store(hash_settings(settings))
    store.sync_outside_edits()
    store.write("docs/a.md", "# A\nText.\n", actor="a")
    assert not (settings.vault / ".git").exists()


def test_fresh_index_does_not_log_the_whole_vault(settings):
    (settings.vault / "docs").mkdir()
    (settings.vault / "docs/old.md").write_text("# Old\nThere before the index.\n")
    store = open_store(hash_settings(settings))
    store.sync_outside_edits()
    assert logged(store) == []
    assert store.index.db.execute("SELECT 1 FROM files WHERE path='docs/old.md'").fetchone()


def test_watcher_ignores_git_and_cortex_state():
    import threading
    from types import SimpleNamespace

    from cortex.watcher import _Handler

    changed = threading.Event()
    handler = _Handler(changed, "/v")
    for path in ("/v/.git/index", "/v/.git/objects/ab/cd", "/v/.cortex/changes.jsonl", "/v/.cortex/index.sqlite"):
        handler.on_any_event(SimpleNamespace(src_path=path, event_type="modified"))
    assert not changed.is_set()
    handler.on_any_event(SimpleNamespace(src_path="/v/memory/x.md", event_type="modified"))
    assert changed.is_set()


def test_existing_git_dir_is_ignored_and_untouched(settings):
    git = settings.vault / ".git"
    (git / "refs").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "notes.md").write_text("# Not a note\n")
    before = {p: p.read_bytes() for p in git.rglob("*") if p.is_file()}
    store = open_store(hash_settings(settings))
    store.sync_outside_edits()
    store.write("docs/a.md", "# A\nText.\n", actor="a")
    store.sync_outside_edits()
    assert {p: p.read_bytes() for p in git.rglob("*") if p.is_file()} == before
    indexed = [r["path"] for r in store.index.db.execute("SELECT path FROM files")]
    assert not [p for p in indexed if p.startswith(".git")]
    assert all(not p.startswith(".") for e in logged(store) for p in e["paths"])
