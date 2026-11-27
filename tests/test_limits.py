import pytest

from cortex.vault import VaultError


def seed(store, path, text):
    """Put a note on disk the way an older Cortex or a hand edit could have left it."""
    full = store.vault.root / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(text)
    store.index.sync()
    return store.vault.hash(path)


# B1: status allow-list


def test_write_refuses_status_outside_the_allow_list(store):
    with pytest.raises(VaultError, match="current, superseded, expired, retired") as refused:
        store.write("entities/app.md", "---\nstatus: v2 in progress\n---\n# App\nThe app.\n", actor="a")
    assert "phase:" in str(refused.value)
    assert not (store.vault.root / "entities/app.md").exists()


def test_update_refuses_status_outside_the_allow_list(store):
    made = store.write("entities/app.md", "# App\nThe app.\n", actor="a")
    with pytest.raises(VaultError, match="allowed"):
        store.update("entities/app.md", {"status": "deployed"}, "shipped", made["hash"], actor="a")
    assert "status: current" in store.vault.read_text("entities/app.md")


def test_replace_refuses_a_new_invalid_status(store):
    made = store.write("docs/plan.md", "# Plan\nv1\n", actor="a")
    with pytest.raises(VaultError, match="allowed"):
        store.write("docs/plan.md", "---\nstatus: draft\n---\n# Plan\nv2\n", made["hash"], actor="a")


@pytest.mark.parametrize("status", ["current", "superseded", "expired", "retired"])
def test_allowed_statuses_pass(store, status):
    store.write("entities/app.md", f"---\nstatus: {status}\n---\n# App\nThe app.\n", actor="a")
    made = store.write("docs/plan.md", "# Plan\nv1\n", actor="a")
    store.update("docs/plan.md", {"status": status}, "check", made["hash"], actor="a")


def test_missing_status_still_defaults_to_current(store):
    store.write("entities/app.md", "# App\nThe app.\n", actor="a")
    assert "status: current" in store.vault.read_text("entities/app.md")
    store.write("docs/plan.md", "# Plan\nNo frontmatter.\n", actor="a")
    assert store.vault.load("docs/plan.md").status == "current"


def test_existing_invalid_status_does_not_block_other_edits(store):
    hash_ = seed(store, "entities/app.md", "---\nstatus: active\nport: 1\n---\n# App\nThe app.\n")
    store.update("entities/app.md", {"port": 2}, "moved", hash_, actor="a")
    assert "port: 2" in store.vault.read_text("entities/app.md")


def test_supersede_onto_a_note_with_an_old_invalid_status_still_works(store):
    store.write("memory/focus.md", "Current focus is gardening.", actor="a")
    seed(store, "memory/beekeeping.md", "---\nstatus: active\n---\nFocus is beekeeping.")
    store.supersede("memory/focus.md", "memory/beekeeping.md", "changed", actor="a")
    assert "status: superseded" in store.vault.read_text("memory/focus.md")
