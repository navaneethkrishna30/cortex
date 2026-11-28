"""The weekly dreaming wrapper (clients/dreamer/dream.py): it must skip an unchanged vault, never
advance its state after a failed run, put back any note the dreamer cut body text from (except a
card shortened to its repo link), leave every other change alone, and turn proposals into one task.

The vault here is a plain folder with a hand-written `.cortex/changes.jsonl`, in the format the
server writes: one {"at", "actor", "action", "paths"} line per change."""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cortex.notes import parse

SCRIPT = Path(__file__).parent.parent / "clients/dreamer/dream.py"
spec = importlib.util.spec_from_file_location("cortex_dream", SCRIPT)
dream = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dream)

REPO = "https://github.com/example/app"
DAY = "2026-11-27"


class Vault:
    """A vault folder plus its change log, edited the way the server would."""

    def __init__(self, root: Path):
        self.root = root
        (root / ".cortex").mkdir(parents=True)

    def log(self, actor, action, paths, at=None):
        at = at or datetime.now(UTC).isoformat()
        with (self.root / dream.CHANGES).open("a") as changes:
            changes.write(json.dumps({"at": at, "actor": actor, "action": action, "paths": paths}) + "\n")

    def write(self, path, text, actor="laptop", action="write", at=None):
        full = self.root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text)
        self.log(actor, action, [path], at)

    def move(self, source, target, actor="dreamer"):
        (self.root / target).parent.mkdir(parents=True, exist_ok=True)
        (self.root / source).rename(self.root / target)
        self.log(actor, "move", [source, target])

    def read(self, path):
        return (self.root / path).read_text()


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path / "vault")


def past(days=1):
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def report(applied=(), proposals=()):
    body = json.dumps({"applied": list(applied), "proposals": list(proposals)})
    return f"Done.\n\n```json\n{body}\n```\n"


def env_for(vault, tmp_path, **extra):
    return {
        "DREAMER_VAULT": str(vault.root),
        "DREAMER_STATE_DIR": str(tmp_path / "state"),
        "DREAMER_CMD": "unused",
        **extra,
    }


def agent_doing(action, output=None):
    """A stand-in for the headless agent: runs `action` (vault edits) and prints a report."""
    calls = []

    def agent(prompt, env):
        calls.append(prompt)
        action()
        return 0, report() if output is None else output

    agent.calls = calls
    return agent


def run(vault, tmp_path, action=lambda: None, output=None, **extra):
    """One run with a fake agent; returns (exit code, tasks sent)."""
    outbox = []
    code = dream.run(env_for(vault, tmp_path, **extra), agent=agent_doing(action, output), sink=outbox.append)
    return code, outbox


# Skip logic and state


def test_first_run_runs_and_stores_its_start(vault, tmp_path):
    vault.write("memory/tea.md", "Prefers green tea.\n", at=past())
    agent = agent_doing(lambda: None)
    assert dream.run(env_for(vault, tmp_path), agent=agent) == 0
    assert len(agent.calls) == 1 and "Cortex" in agent.calls[0]
    assert dream.parse_time((tmp_path / "state/last-run").read_text().strip()) is not None


def test_missing_change_log_runs(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    agent = agent_doing(lambda: None)
    assert dream.run({"DREAMER_VAULT": str(root), "DREAMER_STATE_DIR": str(tmp_path / "state")}, agent=agent) == 0
    assert len(agent.calls) == 1


def test_skips_when_nothing_changed(vault, tmp_path):
    vault.write("memory/tea.md", "Prefers green tea.\n", at=past())
    run(vault, tmp_path)
    agent = agent_doing(lambda: None)
    assert dream.run(env_for(vault, tmp_path), agent=agent) == 0
    assert agent.calls == []


def test_runs_again_after_a_change(vault, tmp_path):
    run(vault, tmp_path)
    vault.write("memory/tea.md", "Prefers green tea.\n")
    agent = agent_doing(lambda: None)
    dream.run(env_for(vault, tmp_path), agent=agent)
    assert len(agent.calls) == 1


def test_own_changes_do_not_trigger_next_week(vault, tmp_path):
    vault.write("memory/tea.md", "---\nstatus: current\n---\nGreen tea until 2026-12-31.\n", at=past())
    fix = lambda: vault.write(
        "memory/tea.md",
        "---\nstatus: current\nexpires: 2026-12-31\n---\nGreen tea until 2026-12-31.\n",
        actor="dreamer",
        action="update",
    )
    run(vault, tmp_path, fix)
    agent = agent_doing(lambda: None)
    dream.run(env_for(vault, tmp_path), agent=agent)
    assert agent.calls == []


def test_state_not_advanced_when_agent_command_fails(vault, tmp_path):
    env = env_for(vault, tmp_path, DREAMER_CMD="sh -c 'cat >/dev/null; exit 3'")
    assert dream.run(env) != 0
    assert not (tmp_path / "state/last-run").exists()


def test_state_not_advanced_without_a_report(vault, tmp_path):
    assert run(vault, tmp_path, output="no json here")[0] != 0
    assert not (tmp_path / "state/last-run").exists()


def test_real_command_gets_the_prompt_on_stdin(vault, tmp_path):
    out = tmp_path / "prompt.txt"
    env = env_for(vault, tmp_path, DREAMER_CMD=f"sh -c 'cat > {out}; printf %s \"$0\"' '{report()}'")
    assert dream.run(env) == 0
    assert out.read_text() == dream.DEFAULT_PROMPT.read_text() + f"\nToday is {dream.today()}.\n"


def test_snapshot_is_removed_after_the_run(vault, tmp_path):
    vault.write("docs/plan.md", "# Plan\n", at=past())
    run(vault, tmp_path)
    assert not list((tmp_path / "state").glob("snapshot-*"))


# The guard


def test_body_cut_by_the_dreamer_is_restored(vault, tmp_path):
    vault.write("docs/plan.md", "# Plan\nStep one.\nStep two.\n", at=past())
    cut = lambda: vault.write("docs/plan.md", "# Plan\nStep one.\n", actor="dreamer")
    code, tasks = run(vault, tmp_path, cut)
    assert code == 0 and vault.read("docs/plan.md") == "# Plan\nStep one.\nStep two.\n"
    title, body = tasks[0]
    assert title == f"Cortex dreaming proposals {dream.today()}"
    assert "## Restored" in body and "`docs/plan.md`" in body


def test_note_deleted_by_the_dreamer_is_restored(vault, tmp_path):
    vault.write("memory/tea.md", "Prefers green tea.\n", at=past())

    def delete():
        (vault.root / "memory/tea.md").unlink()
        vault.log("dreamer", "write", ["memory/tea.md"])

    run(vault, tmp_path, delete)
    assert vault.read("memory/tea.md") == "Prefers green tea.\n"


def test_frontmatter_and_history_changes_are_kept(vault, tmp_path):
    vault.write("entities/app.md", "---\nstatus: current\ndepends_on: db\n---\n# App\nThe app.\n", at=past())
    new = (
        '---\nstatus: current\ndepends_on: "[[entities/db]]"\nexpires: 2027-01-31\n---\n# App\nThe app.\n\n'
        "## History\n- 2026-11-27: expires (unset) -> 2027-01-31 (end date stated)\n"
    )
    code, tasks = run(vault, tmp_path, lambda: vault.write("entities/app.md", new, actor="dreamer", action="update"))
    assert vault.read("entities/app.md") == new and tasks == []


def test_additive_changes_and_link_fixes_are_kept(vault, tmp_path):
    vault.write("docs/plan.md", "# Plan\nSee [[ap]] for details.\n", at=past())

    def work():
        vault.write("docs/plan.md", "# Plan\nSee [[entities/app]] for details.\nMore.\n", actor="dreamer")
        vault.write("logs/raw/2026-11-27.md", "# raw\ndreaming ran\n", actor="dreamer", action="save_log")

    run(vault, tmp_path, work)
    assert vault.read("docs/plan.md") == "# Plan\nSee [[entities/app]] for details.\nMore.\n"


def test_log_archive_with_link_rewrite_is_kept(vault, tmp_path):
    vault.write("logs/daily/2026-01-15.md", "# daily\nold day\n", at=past())
    vault.write("docs/notes.md", "# Notes\nSee [[logs/daily/2026-01-15]].\n", at=past())

    def archive():
        (vault.root / ".archive/logs/daily").mkdir(parents=True)
        (vault.root / "logs/daily/2026-01-15.md").rename(vault.root / ".archive/logs/daily/2026-01-15.md")
        (vault.root / "docs/notes.md").write_text("# Notes\nSee [[.archive/logs/daily/2026-01-15]].\n")
        vault.log("dreamer", "move", ["logs/daily/2026-01-15.md", ".archive/logs/daily/2026-01-15.md", "docs/notes.md"])

    code, tasks = run(vault, tmp_path, archive)
    assert (vault.root / ".archive/logs/daily/2026-01-15.md").is_file()
    assert not (vault.root / "logs/daily/2026-01-15.md").exists()
    assert "[[.archive/logs/daily/2026-01-15]]" in vault.read("docs/notes.md") and tasks == []


def test_archiving_a_live_note_is_left_and_reported(vault, tmp_path):
    vault.write("memory/tea.md", "Prefers green tea.\n", at=past())
    code, tasks = run(vault, tmp_path, lambda: vault.move("memory/tea.md", ".archive/memory/tea.md"))
    assert (vault.root / ".archive/memory/tea.md").is_file() and not (vault.root / "memory/tea.md").exists()
    assert "- [ ] `memory/tea.md` -> `.archive/memory/tea.md`" in tasks[0][1]


def test_move_is_read_from_the_files_not_the_path_order(vault, tmp_path):
    vault.write("memory/tea.md", "Prefers green tea.\n", at=past())

    def move_reversed():
        (vault.root / ".archive/memory").mkdir(parents=True)
        (vault.root / "memory/tea.md").rename(vault.root / ".archive/memory/tea.md")
        vault.log("dreamer", "move", [".archive/memory/tea.md", "memory/tea.md"])

    code, tasks = run(vault, tmp_path, move_reversed)
    assert not (vault.root / "memory/tea.md").exists()
    assert "- [ ] `memory/tea.md` -> `.archive/memory/tea.md`" in tasks[0][1]


def test_move_then_cut_is_restored_at_the_new_path(vault, tmp_path):
    vault.write("entities/app.md", "# App\nThe app.\nRuns on the server.\n", at=past())

    def work():
        vault.move("entities/app.md", "entities/service.md")
        vault.write("entities/service.md", "# App\nThe app.\n", actor="dreamer")

    code, tasks = run(vault, tmp_path, work)
    assert vault.read("entities/service.md") == "# App\nThe app.\nRuns on the server.\n"
    assert "`entities/service.md`" in tasks[0][1] and "-> `entities/service.md`" in tasks[0][1]


def test_log_archive_that_lost_text_is_restored(vault, tmp_path):
    vault.write("logs/daily/2026-01-15.md", "# daily\nold day\n", at=past())

    def archive_and_cut():
        vault.move("logs/daily/2026-01-15.md", ".archive/logs/daily/2026-01-15.md")
        (vault.root / ".archive/logs/daily/2026-01-15.md").write_text("# daily\n")

    run(vault, tmp_path, archive_and_cut)
    assert vault.read(".archive/logs/daily/2026-01-15.md") == "# daily\nold day\n"


def test_failed_restore_keeps_the_snapshot_and_state(vault, tmp_path):
    vault.write("docs/plan.md", "# Plan\nStep one.\nStep two.\n", at=past())

    def cut_and_lock():
        vault.write("docs/plan.md", "# Plan\nStep one.\n", actor="dreamer")
        (vault.root / "docs/plan.md").chmod(0o444)

    code, tasks = run(vault, tmp_path, cut_and_lock)
    kept = list((tmp_path / "state").glob("snapshot-*"))
    assert code != 0 and len(kept) == 1
    assert (kept[0] / "docs/plan.md").read_text() == "# Plan\nStep one.\nStep two.\n"
    assert "Restore by hand" in tasks[0][1] and str(kept[0]) in tasks[0][1]
    assert not (tmp_path / "state/last-run").exists()


def test_a_change_logged_in_the_start_second_is_checked(vault, tmp_path):
    vault.write("docs/plan.md", "# Plan\nStep one.\nStep two.\n", at=past())

    def cut_at_start():
        moment = datetime.now(UTC).replace(microsecond=0).isoformat()
        vault.write("docs/plan.md", "# Plan\nStep one.\n", actor="dreamer", at=moment)

    run(vault, tmp_path, cut_at_start)
    assert vault.read("docs/plan.md") == "# Plan\nStep one.\nStep two.\n"


def test_supersede_is_kept_but_flagged(vault, tmp_path):
    vault.write("memory/tea.md", "---\nstatus: current\n---\nPrefers green tea.\n", at=past())
    swap = lambda: vault.write(
        "memory/tea.md", "---\nstatus: superseded\n---\nPrefers green tea.\n", actor="dreamer", action="supersede"
    )
    code, tasks = run(vault, tmp_path, swap)
    assert "status: superseded" in vault.read("memory/tea.md")
    assert "propose-only" in tasks[0][1]


def test_other_actors_are_ignored(vault, tmp_path):
    vault.write("docs/plan.md", "# Plan\nStep one.\nStep two.\n", at=past())
    run(vault, tmp_path, lambda: vault.write("docs/plan.md", "# Plan\nStep one.\n", actor="laptop"))
    assert vault.read("docs/plan.md") == "# Plan\nStep one.\n"


def test_dreamer_changes_before_the_run_are_ignored(vault, tmp_path):
    vault.write("docs/plan.md", "# Plan\nStep one.\n", actor="dreamer", at=past())
    run(vault, tmp_path)
    assert vault.read("docs/plan.md") == "# Plan\nStep one.\n"


def test_paths_outside_the_vault_are_never_written(vault, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("mine\n")
    vault.write("docs/plan.md", "# Plan\n", at=past())
    code, tasks = run(vault, tmp_path, lambda: vault.log("dreamer", "write", ["../outside.md", str(outside)]))
    assert outside.read_text() == "mine\n" and "unsafe path" in tasks[0][1]


def long_card(detail_lines):
    detail = "".join(f"Detail line {n}: how the service is built and run, at length.\n" for n in range(detail_lines))
    return f"---\nstatus: current\nkind: repo\n---\n# App\nThe app. Code: {REPO}\n{detail}"


def test_card_shortened_to_its_repo_link_is_kept(vault, tmp_path):
    vault.write("entities/app.md", long_card(30), at=past())
    short = (
        f"---\nstatus: current\nkind: repo\n---\n# App\nThe app. Code: {REPO}\nHow it is built and run: {REPO}#readme\n"
    )
    run(vault, tmp_path, lambda: vault.write("entities/app.md", short, actor="dreamer"))
    assert vault.read("entities/app.md") == short


def test_card_shortened_without_its_repo_link_is_restored(vault, tmp_path):
    vault.write("entities/app.md", long_card(30), at=past())
    short = "---\nstatus: current\nkind: repo\n---\n# App\nThe app. Code: https://example.org/other\n"
    run(vault, tmp_path, lambda: vault.write("entities/app.md", short, actor="dreamer"))
    assert vault.read("entities/app.md") == long_card(30)


def test_cutting_a_card_already_under_the_limit_is_restored(vault, tmp_path):
    vault.write("entities/app.md", long_card(3), at=past())
    short = f"---\nstatus: current\nkind: repo\n---\n# App\nThe app. Code: {REPO}\n"
    run(vault, tmp_path, lambda: vault.write("entities/app.md", short, actor="dreamer"))
    assert vault.read("entities/app.md") == long_card(3)


@pytest.mark.parametrize(
    "text",
    [
        "no frontmatter\nline\n",
        "---\nstatus: current\n---\n# T\nBody.\n\n## History\n- x\n",
        "---\nstatus: current\n---\n# T\nA\n\n## History\n- x\n\n## Later\nB\n",
        "---\n---\nBody\n",
    ],
)
def test_body_split_matches_the_server(text):
    assert dream.body_without_history(text) == parse(text).body_without_history


# Report parsing, caps and the task


def test_report_parsing_takes_the_last_json_block():
    text = '```json\n{"applied": [], "proposals": [1]}\n```\nthen\n' + report(
        proposals=[{"kind": "merge", "paths": ["a.md", "b.md"], "reason": "same"}]
    )
    parsed = dream.parse_report(text)
    assert parsed["proposals"] == [{"kind": "merge", "paths": ["a.md", "b.md"], "reason": "same"}]
    assert dream.parse_report("nothing") is None


def test_proposals_are_capped_and_warned():
    proposals = [{"kind": "merge", "paths": [f"memory/{n}.md"], "reason": "dup"} for n in range(25)]
    applied = [{"path": f"memory/{n}.md", "change": "link"} for n in range(12)]
    kept, warnings = dream.enforce_caps({"applied": applied, "proposals": proposals}, dreamer_changes=11)
    assert len(kept) == 20
    assert any("25 proposals" in w for w in warnings) and any("12 changes" in w for w in warnings)
    assert any("11 dreamer changes" in w for w in warnings)


def test_task_has_one_checkbox_per_proposal():
    proposals = [
        {"kind": "merge", "paths": ["memory/a.md", "memory/b.md"], "reason": "same fact"},
        {"kind": "move-to-repo", "paths": ["entities/app.md"], "reason": "build steps"},
    ]
    title, body = dream.format_task(DAY, proposals)
    assert title == f"Cortex dreaming proposals {DAY}"
    boxes = [line for line in body.splitlines() if line.startswith("- [ ] ")]
    assert boxes == [
        "- [ ] **merge** `memory/a.md`, `memory/b.md`: same fact",
        "- [ ] **move-to-repo** `entities/app.md`: build steps",
    ]


def test_no_task_when_nothing_to_say(vault, tmp_path):
    assert run(vault, tmp_path)[1] == []


def test_file_sink_is_the_default(vault, tmp_path):
    proposals = [{"kind": "merge", "paths": ["memory/a.md"], "reason": "dup"}]
    dream.run(env_for(vault, tmp_path), agent=agent_doing(lambda: None, report(proposals=proposals)))
    written = list((tmp_path / "state").glob("proposals-*.md"))
    assert len(written) == 1 and "- [ ] **merge** `memory/a.md`: dup" in written[0].read_text()


def test_failed_sink_falls_back_to_a_file_and_keeps_state(vault, tmp_path):
    def broken(task):
        raise OSError("tracker down")

    proposals = [{"kind": "merge", "paths": ["memory/a.md"], "reason": "dup"}]
    agent = agent_doing(lambda: None, report(proposals=proposals))
    assert dream.run(env_for(vault, tmp_path), agent=agent, sink=broken) != 0
    assert list((tmp_path / "state").glob("proposals-*.md"))
    assert not (tmp_path / "state/last-run").exists()


def test_vikunja_sink_puts_one_task():
    sent = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"id": 7}'

    def opener(request, timeout):
        sent.append(request)
        return Response()

    sink = dream.VikunjaSink("https://tracker.example/", "tok", "42", opener=opener)
    sink(("Cortex dreaming proposals 2026-11-27", "- [ ] **merge** `memory/a.md`: a <b> & c"))
    request = sent[0]
    assert request.get_method() == "PUT"
    assert request.full_url == "https://tracker.example/api/v1/projects/42/tasks"
    assert request.get_header("Authorization") == "Bearer tok"
    payload = json.loads(request.data)
    assert payload["title"] == "Cortex dreaming proposals 2026-11-27"
    assert 'data-type="taskItem"' in payload["description"] and "a &lt;b&gt; &amp; c" in payload["description"]


def test_sink_from_env_needs_all_three_vikunja_settings(tmp_path):
    assert isinstance(dream.sink_from_env({"VIKUNJA_URL": "https://x.example"}, tmp_path), dream.FileSink)
    full = {"VIKUNJA_URL": "https://x.example", "VIKUNJA_TOKEN": "t", "VIKUNJA_PROJECT_ID": "3"}
    assert isinstance(dream.sink_from_env(full, tmp_path), dream.VikunjaSink)
