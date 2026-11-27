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


# B2: size limits


def words(n, seed="w"):
    """Distinct filler of exactly n characters, so the duplicate check never fires."""
    text = " ".join(f"{seed}{i}" for i in range(n))
    return text[:n].rstrip() + "x" * (n - len(text[:n].rstrip()))


def test_memory_over_the_limit_refused(store):
    store.write("memory/short.md", words(300, "a"), actor="a")
    with pytest.raises(VaultError, match="move detail to the repo; link it"):
        store.write("memory/long.md", words(301, "b"), actor="a")


def test_entity_over_the_limit_refused(store):
    store.write("entities/ok.md", f"# Ok\n{words(1190, 'c')}\n", actor="a")
    with pytest.raises(VaultError, match="move detail to the repo; link it"):
        store.write("entities/big.md", f"# Big\n{words(1300, 'd')}\n", actor="a")


def test_history_and_frontmatter_do_not_count(store):
    history = "\n".join(f"- 2026-01-0{i % 9 + 1}: port {i} -> {i + 1} (moved)" for i in range(60))
    text = f"---\nstatus: current\nport: 1\n---\n# App\n{words(1100, 'e')}\n\n## History\n{history}\n"
    store.write("entities/app.md", text, actor="a")


@pytest.mark.parametrize("path", ["docs/long.md", "core/profile.md"])
def test_docs_and_core_are_exempt(store, path):
    store.write(path, f"# Long\n{words(5000, 'f')}\n", actor="human", human=True)


def test_logs_are_exempt(store):
    store.save_log("raw", words(5000, "g"), actor="a")


def test_oversized_card_can_still_be_updated_and_shrunk(store):
    hash_ = seed(store, "entities/app.md", f"---\nstatus: current\nport: 1\n---\n# App\n{words(2000, 'h')}\n")
    made = store.update("entities/app.md", {"port": 2}, "moved", hash_, actor="a")
    text = store.vault.read_text("entities/app.md")
    shorter = text.replace(words(2000, "h"), words(1500, "h"))
    made = store.write("entities/app.md", shorter, made["hash"], actor="a")
    with pytest.raises(VaultError, match="move detail to the repo"):
        store.write("entities/app.md", shorter.replace(words(1500, "h"), words(1600, "h")), made["hash"], actor="a")


def test_supersede_with_oversized_text_refused(store):
    store.write("memory/focus.md", "Current focus is gardening.", actor="a")
    with pytest.raises(VaultError, match="move detail to the repo"):
        store.supersede("memory/focus.md", words(400, "i"), "changed", actor="a")
    assert "status: current" in store.vault.read_text("memory/focus.md")


def test_limits_come_from_vault_config(tmp_path):
    from cortex.config import load_settings

    defaults = load_settings(tmp_path)
    assert (defaults.memory_body_limit, defaults.entity_body_limit) == (300, 1200)
    (tmp_path / ".cortex").mkdir()
    (tmp_path / ".cortex/config.toml").write_text("memory_body_limit = 200\nentity_body_limit = 800\n")
    settings = load_settings(tmp_path)
    assert (settings.memory_body_limit, settings.entity_body_limit) == (200, 800)


def test_limit_setting_is_used(store):
    store.vault.settings.memory_body_limit = 20
    with pytest.raises(VaultError, match="20 characters"):
        store.write("memory/x.md", "A fact that is longer than twenty.", actor="a")
