import pytest

from cortex.search import search
from cortex.vault import VaultError

CARD = "---\nstatus: current\nlocated_in: [[server]]\nport: 8080\n---\n# App\nThe app.\n"


def last_author(store):
    return store.git.run("log", "-1", "--format=%an").strip()


def test_write_commits_and_names_the_token(store):
    store.write("memory/likes-postgres.md", "Prefers Postgres.", actor="laptop")
    assert last_author(store) == "laptop"
    assert "status: current" in store.vault.read_text("memory/likes-postgres.md")


def test_core_and_logs_refused_for_agents(store):
    with pytest.raises(VaultError):
        store.write("core/rules.md", "x", actor="a")
    with pytest.raises(VaultError):
        store.write("logs/daily/x.md", "x", actor="a")


@pytest.mark.parametrize("path", ["../x.md", "memory/../../x.md", "/etc/passwd", ".git/config", ".cortex/config.toml", "other/x.md"])
def test_path_traversal_refused(store, path):
    with pytest.raises(VaultError):
        store.write(path, "x", actor="a")


def test_symlink_escape_refused(store, tmp_path):
    (store.vault.root / "docs").mkdir()
    (store.vault.root / "docs/out").symlink_to(tmp_path)
    with pytest.raises(VaultError):
        store.write("docs/out/x.md", "x", actor="a")


def test_second_note_on_subject_refused_and_returned(store):
    store.write("entities/app.md", CARD, actor="a")
    with pytest.raises(VaultError, match="entities/app.md already covers"):
        store.write("memory/app.md", "Another app line.", actor="a")
    with pytest.raises(VaultError, match="already covers"):
        store.write("entities/other.md", "---\nstatus: current\n---\n# App\nDup.\n", actor="a")


def test_multi_line_memory_refused(store):
    with pytest.raises(VaultError, match="one sentence"):
        store.write("memory/x.md", "one\ntwo", actor="a")


def test_replace_needs_fresh_hash(store):
    made = store.write("docs/plan.md", "# Plan\nv1\n", actor="a")
    with pytest.raises(VaultError, match="stale"):
        store.write("docs/plan.md", "# Plan\nv2\n", "bad", actor="a")
    store.write("docs/plan.md", "# Plan\nv2\n", made["hash"], actor="a")
    assert "v2" in store.vault.read_text("docs/plan.md")


def test_unknown_relation_refused(store):
    with pytest.raises(VaultError, match="unknown relation"):
        store.write("entities/x.md", "---\nstatus: current\ninvented: [[y]]\n---\n# X\n", actor="a")


def test_update_edits_in_place_keeps_history_and_finds_stale_copies(store):
    made = store.write("entities/app.md", CARD, actor="a")
    store.write("docs/notes.md", "# Notes\nThe app runs on port 8080 today.\n", actor="a")
    result = store.update("entities/app.md", {"port": 9090}, "moved", made["hash"], actor="b")
    text = store.vault.read_text("entities/app.md")
    assert "port: 9090" in text and "port 8080 -> 9090 (moved)" in text
    assert "located_in: [[server]]" in text
    assert [s["path"] for s in result["stale_copies"]] == ["docs/notes.md"]
    assert last_author(store) == "b"


def test_update_unchanged_is_a_no_op(store):
    made = store.write("entities/app.md", CARD, actor="a")
    head = store.git.run("rev-parse", "HEAD")
    assert store.update("entities/app.md", {"port": 8080}, "same", made["hash"], actor="a")["commit"] is None
    assert store.git.run("rev-parse", "HEAD") == head


def test_moving_relation_flips_backlinks(store):
    store.write("entities/server.md", "# Server\nBox.\n", actor="a")
    store.write("entities/laptop.md", "# Laptop\nPortable.\n", actor="a")
    made = store.write("entities/app.md", CARD, actor="a")
    assert store.read("entities/server.md")["backlinks"] == [{"src": "entities/app.md", "relation": "located_in"}]
    store.update("entities/app.md", {"located_in": "[[laptop]]"}, "moved", made["hash"], actor="a")
    assert store.read("entities/server.md")["backlinks"] == []
    assert store.read("entities/laptop.md")["backlinks"][0]["src"] == "entities/app.md"


def test_supersede_with_text_hides_old_from_search_and_context(store):
    store.write("memory/focus.md", "Current focus is gardening.", actor="a")
    head_count = int(store.git.run("rev-list", "--count", "HEAD"))
    result = store.supersede("memory/focus.md", "Current focus is beekeeping.", "changed", actor="a")
    assert int(store.git.run("rev-list", "--count", "HEAD")) == head_count + 1
    old = store.read("memory/focus.md")
    assert "status: superseded" in old["content"] and "superseded by" in old["history"]
    found = search(store.index, "current focus", None)
    paths = [r["path"] for r in found["results"]]
    assert result["path"] in paths and "memory/focus.md" not in paths
    assert {"path": "memory/focus.md", "reason": "status: superseded"} in found["excluded"]
    context = store.read("@context")["content"]
    assert "beekeeping" in context and "gardening" not in context


def test_move_rewrites_links_and_archive_forgets(store):
    store.write("entities/app.md", CARD, actor="a")
    store.write("docs/a.md", "# A\nSee [[app]] and [[entities/app|the app]].\n", actor="a")
    store.move("entities/app.md", "entities/service.md", actor="a")
    assert store.vault.read_text("docs/a.md") == "# A\nSee [[entities/service]] and [[entities/service|the app]].\n"
    assert not (store.vault.root / "entities/app.md").exists()
    store.move("entities/service.md", ".archive/service.md", actor="a")
    assert not [r for r in search(store.index, "app", None)["results"] if "service" in r["path"]]
    assert store.git.dirty_paths() == []


def test_archive_stays_out_of_every_search_but_still_opens(store):
    store.write("docs/zebras.md", "# Zebras\nStriped zebras live here.\n", actor="a")
    store.move("docs/zebras.md", ".archive/docs/zebras.md", actor="a")
    for scope in (None, "all"):
        for exact in (False, True):
            found = search(store.index, "zebras", scope, exact)
            assert not found["results"] and not found["excluded"]
    assert "Striped" in store.read(".archive/docs/zebras.md")["content"]


def test_sync_clears_archived_notes_already_in_the_index(store):
    store.write("docs/zebras.md", "# Zebras\nStriped zebras live here.\n", actor="a")
    store.index.db.execute("UPDATE files SET path='.archive/zebras.md', folder='.archive' WHERE path='docs/zebras.md'")
    store.index.db.execute("UPDATE chunks SET path='.archive/zebras.md' WHERE path='docs/zebras.md'")
    store.index.db.execute("UPDATE chunks_fts SET path='.archive/zebras.md' WHERE path='docs/zebras.md'")
    store.index.sync()
    assert not [r for r in search(store.index, "zebras", "all")["results"] if r["path"].startswith(".archive")]


def test_search_drops_weak_results_and_core(store):
    (store.vault.root / "core/profile.md").write_text("# Profile\nOwns a laptop.\n")
    store.index.sync()
    store.write("memory/laptop.md", "The laptop runs Linux.", actor="a")
    for name in ("oven", "garden", "piano"):
        store.write(f"memory/{name}.md", f"The {name} is in the house.", actor="a")
    paths = [r["path"] for r in search(store.index, "laptop")["results"]]
    assert paths == ["memory/laptop.md"]
    assert not search(store.index, "laptop", "all", exact=True)["results"][1:]
    assert not [r for r in search(store.index, "profile", "all")["results"] if r["path"].startswith("core/")]


def test_search_returns_eight_results_by_default(store):
    for n in range(12):
        store.write(f"entities/box-{n}.md", f"# Box {n}\nA zebra lives here.\n", actor="a")
    assert len(search(store.index, "zebra")["results"]) == 8


def test_update_can_change_status_with_a_history_line(store):
    made = store.write("docs/guide.md", "---\nstatus: unread\n---\n# Guide\nStriped zebras.\n", actor="a")
    assert not search(store.index, "zebras", "all")["results"]
    store.update("docs/guide.md", {"status": "current"}, "read it", made["hash"], actor="a")
    assert "status unread -> current (read it)" in store.vault.read_text("docs/guide.md")
    assert search(store.index, "zebras", "all")["results"]


def test_update_still_refuses_superseded_by(store):
    made = store.write("docs/guide.md", "# Guide\nv1\n", actor="a")
    with pytest.raises(VaultError, match="supersede"):
        store.update("docs/guide.md", {"superseded_by": "[[x]]"}, "no", made["hash"], actor="a")


def test_save_log_can_backfill_a_date(store):
    made = store.save_log("daily", "old note", "2026-01-15", actor="a")
    assert made["path"] == "logs/daily/2026-01-15.md"
    text = store.vault.read_text(made["path"])
    assert text.startswith("# daily 2026-01-15\n") and "## added " in text and "old note" in text


@pytest.mark.parametrize("bad", ["yesterday", "2026-13-01", "../../x", "2026-01-15/../x"])
def test_save_log_refuses_a_bad_date(store, bad):
    with pytest.raises(VaultError, match="YYYY-MM-DD"):
        store.save_log("daily", "note", bad, actor="a")


def test_move_reports_live_notes_still_linking(store):
    store.write("docs/guide.md", "# Guide\nv1\n", actor="a")
    store.write("entities/app.md", "# App\nSee [[docs/guide]].\n", actor="a")
    store.write("docs/other.md", "# Other\nNo links.\n", actor="a")
    result = store.move("docs/guide.md", ".archive/docs/guide.md", actor="a")
    assert result["linked_from"] == ["entities/app.md"]
    assert "[[.archive/docs/guide]]" in store.vault.read_text("entities/app.md")


def test_save_log_appends_and_search_reports_it(store):
    store.save_log("daily", "talked about zebras", actor="a")
    store.save_log("daily", "more zebras", actor="a")
    path = store.list("logs")["files"][0]
    assert store.vault.read_text(path).count("zebras") == 2
    found = search(store.index, "zebras", None)
    assert not [r for r in found["results"] if r["path"].startswith("logs/")]
    assert found["excluded"][0]["folder"] == "logs/"
    assert search(store.index, "zebras", "all")["results"]


@pytest.mark.parametrize("secret", ["ghp_" + "a" * 36, "sk-ant-" + "b" * 30, "-----BEGIN RSA PRIVATE KEY-----", "password: hunter2hunter2"])
def test_secrets_refused_everywhere(store, secret):
    with pytest.raises(VaultError, match="secret"):
        store.write("docs/x.md", secret, actor="a")
    with pytest.raises(VaultError, match="secret"):
        store.save_log("raw", secret, actor="a")


def test_outside_edit_committed_as_human(store):
    (store.vault.root / "docs").mkdir()
    (store.vault.root / "docs/hand.md").write_text("# Hand\nEdited over SSH\n")
    assert store.sync_outside_edits() == ["docs/hand.md"]
    assert last_author(store) == "human"
    assert search(store.index, "SSH", "all")["results"]


def test_index_mismatch_refuses_until_rebuild(store):
    from cortex.embed import HashEmbedder
    from cortex.index import Index, IndexMismatch

    with pytest.raises(IndexMismatch):
        Index(store.vault, HashEmbedder(dimensions=64))


def test_similar_cards_for_different_things_are_allowed(store):
    store.write("entities/box-a.md", "# Box A\nA test machine.\n", actor="a")
    store.write("entities/box-b.md", "# Box B\nA test machine.\n", actor="a")
