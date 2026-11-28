#!/usr/bin/env python3
"""Weekly dreaming: run a headless agent over the vault with its own `dreamer` token, then check
what it changed and turn its proposals into one task (SPEC.md §5). Install: README.md here.

The agent works only through the Cortex MCP tools, and the server records every change in
`<vault>/.cortex/changes.jsonl` with the token's name as `actor`. The prompt tells the agent never
to remove body text, but a prompt is not a guarantee, so this script snapshots the notes before
the run and afterwards compares every file the dreamer touched: one that lost body lines, outside
the one allowed case (a card over the limit shortened to the repo link it already had), is put
back from the snapshot. The check is deliberately conservative: a needless restore costs one
week's tidy-up, a missed one loses the owner's text.

Standard library only, so it runs anywhere the vault is readable without a venv.
"""

from __future__ import annotations

import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

DEFAULT_PROMPT = Path(__file__).with_name("prompt.md")
DEFAULT_STATE = "~/.local/state/cortex-dreamer"
CHANGES = ".cortex/changes.jsonl"
ACTOR = "dreamer"
APPLY_CAP, PROPOSE_CAP = 10, 20
ENTITY_LIMIT = 1200  # the server's default entity_body_limit; override with DREAMER_ENTITY_LIMIT
SNAPSHOT_FOLDERS = ("memory", "entities", "docs", "logs")
ARCHIVE = ".archive/"

# Same patterns as src/cortex/notes.py, copied so this script needs no Cortex install.
LINK = re.compile(r"\[\[([^\]|#\n]+)(?:[#|][^\]\n]*)?\]\]")
FRONTMATTER = re.compile(r"\A---\n(.*?\n)?---\n?", re.DOTALL)
HISTORY = re.compile(r"^## History[ \t]*$", re.MULTILINE)
NEXT_HEADING = re.compile(r"^#{1,2} ", re.MULTILINE)
URL = re.compile(r"https?://[^\s)>\]]+")
JSON_BLOCK = re.compile(r"```json[ \t]*\n(.*?)\n```", re.DOTALL)


def log(message: str) -> None:
    print(f"cortex-dreamer: {message}", file=sys.stderr)


def today() -> str:
    return date.today().isoformat()


def now() -> datetime:
    return datetime.now(UTC)


# The change log


def parse_time(value) -> datetime | None:
    """ISO-8601 from the change log; a time without a zone is UTC."""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def read_changes(vault: Path) -> list[dict] | None:
    """Every well-formed entry of the change log, or None when there is no log."""
    path = vault / CHANGES
    if not path.is_file():
        return None
    entries = []
    for line in path.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entry["when"] = parse_time(entry.get("at"))
            entries.append(entry)
    return entries


def changed_since(entries: list[dict] | None, since: datetime | None, actor: str) -> bool:
    """Whether anyone but the dreamer changed the vault after the last run.

    The dreamer's own changes don't count, or every run would trigger the next. An entry with an
    unreadable time counts, because skipping a needed run is the worse mistake.
    """
    if entries is None or since is None:
        return True
    return any(e.get("actor") != actor and (e["when"] is None or e["when"] > since) for e in entries)


def dreamer_entries(entries: list[dict] | None, start: datetime, actor: str) -> list[dict]:
    """The dreamer's entries from this run; unreadable times are included so they get checked."""
    return [e for e in entries or [] if e.get("actor") == actor and (e["when"] is None or e["when"] >= start)]


def safe_path(path) -> str | None:
    """A vault-relative path from the log, or None if it could point outside the vault."""
    if not isinstance(path, str) or not path or "\\" in path or "\0" in path:
        return None
    parts = PurePosixPath(path).parts
    if PurePosixPath(path).is_absolute() or ".." in parts or not parts:
        return None
    return "/".join(parts)


# What counts as removing body text


def body_without_history(text: str) -> str:
    """The note body as the server measures it: after frontmatter, History section left out."""
    match = FRONTMATTER.match(text)
    body = text[match.end() :] if match else text
    found = HISTORY.search(body)
    if not found:
        return body
    following = NEXT_HEADING.search(body, found.end())
    return body[: found.start()] + (body[following.start() :] if following else "")


def mask_links(line: str) -> str:
    """A link fix changes only what is inside [[ ]]; masking it makes the old and new line equal."""
    return LINK.sub("[[]]", line)


def _lines(body: str) -> Counter:
    return Counter(line.strip() for line in body.splitlines() if line.strip())


def removed_body_lines(old: str, new: str, path: str, entity_limit: int = ENTITY_LIMIT) -> list[str]:
    """Body lines of `old` that `new` lost, other than link-only edits and an allowed card cut."""
    old_body, new_body = body_without_history(old), body_without_history(new)
    old_lines, new_lines = _lines(old_body), _lines(new_body)
    added = list((new_lines - old_lines).elements())
    lost = []
    for line in (old_lines - new_lines).elements():
        twin = next((a for a in added if mask_links(a) == mask_links(line)), None)
        if twin is None:
            lost.append(line)
        else:
            added.remove(twin)
    if lost and _shortened_to_repo_link(path, old_body, new_body, added, entity_limit):
        return []
    return lost


def _shortened_to_repo_link(path: str, old_body: str, new_body: str, added: list[str], limit: int) -> bool:
    """The one case where body text may go: a card over the limit, brought under it, keeping a repo
    link it already had, with every new line pointing at that link (so no text was rewritten)."""
    if not path.startswith("entities/"):
        return False
    if not len(old_body.strip()) > limit >= len(new_body.strip()):
        return False
    links = URL.findall(old_body)
    if not any(link in new_body for link in links):
        return False
    return all(any(link in line for link in links) for line in added)


# Snapshot and guard


def take_snapshot(vault: Path, state_dir: Path) -> Path:
    """Copy the note folders aside; core/ is human-only and .archive/ is never edited in place."""
    snapshot = Path(tempfile.mkdtemp(prefix="snapshot-", dir=state_dir))
    for folder in SNAPSHOT_FOLDERS:
        if (vault / folder).is_dir():
            shutil.copytree(vault / folder, snapshot / folder, symlinks=True)
    return snapshot


def _is_log_archive(source: str, target: str) -> bool:
    return source.startswith("logs/") and target == ARCHIVE + source


def _move_pair(vault: Path, snapshot: Path, paths: list[str], origin: dict) -> tuple[str, str] | None:
    """(source, target) of a move, worked out from the files rather than the order of `paths`:
    the source is the one path that is gone, the target the one that is new. None if unclear."""
    gone = [p for p in paths if not (vault / p).exists() and ((snapshot / p).is_file() or p in origin)]
    new = [p for p in paths if (vault / p).is_file() and not (snapshot / p).exists()]
    return (gone[0], new[0]) if len(gone) == 1 and len(new) == 1 else None


def guard(vault: Path, snapshot: Path, entries: list[dict], entity_limit: int = ENTITY_LIMIT) -> tuple[list, list, list, list]:
    """Check the net effect of the run on every file the dreamer touched.

    Returns (restored, moves, failed, warnings). A note that lost body text, or vanished other
    than by a move, is written back from the snapshot. A moved note is compared with its
    pre-run source, so a move followed by a cut is caught too. A move other than logs/ to
    .archive/ is left where it is and reported: moving it back here would leave two copies.
    A restore that fails is listed in `failed`; the caller then keeps the snapshot.
    """
    touched, origin, moved_away, moves, warnings = [], {}, set(), [], []
    for entry in entries:
        paths = [p for p in map(safe_path, entry.get("paths") or []) if p]
        if len(paths) < len(entry.get("paths") or []):
            warnings.append(f"ignored an unsafe path in a {entry.get('action')} entry")
        if entry.get("action") == "supersede":
            warnings.append(f"propose-only action applied: supersede {', '.join(paths)}")
        pair = _move_pair(vault, snapshot, paths, origin) if entry.get("action") == "move" else None
        if pair:
            source, target = pair
            moved_away.add(source)
            origin[target] = origin.pop(source, source)
            if not _is_log_archive(origin[target], target):
                moves.append({"from": origin[target], "to": target})
            paths = [p for p in paths if p != source]
        touched += [p for p in paths if p not in touched]

    restored, failed = [], []
    for path in touched:
        if path in moved_away:
            continue  # checked under its new path
        before = snapshot / origin.get(path, path)
        if not before.is_file():
            continue  # a new file only adds
        current = vault / path
        try:
            if not current.is_file():
                reason = "note removed"
            else:
                lost = removed_body_lines(before.read_text(), current.read_text(), path, entity_limit)
                if not lost:
                    continue
                reason = f"lost {len(lost)} body line(s), e.g. {lost[0][:60]!r}"
            current.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(before, current)
            restored.append({"path": path, "reason": reason})
        except OSError as error:
            failed.append({"path": path, "reason": str(error)})
    return restored, moves, failed, warnings


# The agent's report


def parse_report(text: str) -> dict | None:
    """The last ```json block holding an object with `applied` and `proposals` lists."""
    for block in reversed(JSON_BLOCK.findall(text or "")):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("applied"), list) and isinstance(data.get("proposals"), list):
            return data
    return None


def _valid_proposal(item) -> bool:
    return (isinstance(item, dict) and isinstance(item.get("kind"), str) and isinstance(item.get("reason"), str)
            and isinstance(item.get("paths"), list) and all(isinstance(p, str) for p in item["paths"]))


def enforce_caps(report: dict, dreamer_changes: int) -> tuple[list[dict], list[str]]:
    """Keep at most 20 well-formed proposals. Applied changes are already in the vault, so an
    overrun there can only be reported."""
    warnings = []
    applied = len(report.get("applied", []))
    if applied > APPLY_CAP:
        warnings.append(f"agent reported {applied} changes applied; the cap is {APPLY_CAP}")
    if dreamer_changes > APPLY_CAP:
        warnings.append(f"the change log shows {dreamer_changes} dreamer changes; the cap is {APPLY_CAP}")
    raw = report.get("proposals", [])
    proposals = [item for item in raw if _valid_proposal(item)]
    if len(proposals) < len(raw):
        warnings.append(f"dropped {len(raw) - len(proposals)} malformed proposal(s)")
    if len(proposals) > PROPOSE_CAP:
        warnings.append(f"agent made {len(proposals)} proposals; kept the first {PROPOSE_CAP}")
    return proposals[:PROPOSE_CAP], warnings


def format_task(day: str, proposals: list[dict], restored=(), moves=(), warnings=(), failed=(),
                snapshot: Path | None = None) -> tuple[str, str]:
    """One task per run: a checkbox per proposal, then what the guard did."""
    lines = ["Proposals from the weekly Cortex dreaming run. Tick the ones you accept and carry them out "
             "in a session; dreaming never applies them itself."]
    if proposals:
        lines += ["", "## Proposals"]
        for item in proposals:
            paths = ", ".join(f"`{path}`" for path in item["paths"])
            lines.append(f"- [ ] **{item['kind']}** {paths}: {item['reason']}")
    if restored:
        lines += ["", "## Restored", "Dreamer edits that removed body text; the note is back as it was before the run."]
        lines += [f"- `{e['path']}`: {e['reason']}" for e in restored]
    if failed:
        lines += ["", "## Restore by hand", f"These lost body text but could not be written back. "
                  f"The pre-run copies are kept in `{snapshot}`."]
        lines += [f"- [ ] `{e['path']}`: {e['reason']}" for e in failed]
    if moves:
        lines += ["", "## Moves to review", "Moves dreaming should only have proposed. Keep them, or move the note back."]
        lines += [f"- [ ] `{m['from']}` -> `{m['to']}`" for m in moves]
    if warnings:
        lines += ["", "## Warnings"] + [f"- {w}" for w in warnings]
    return f"Cortex dreaming proposals {day}", "\n".join(lines) + "\n"


# Where the task goes


class FileSink:
    """Default: the task as a markdown file in the state dir, for the owner to read."""

    def __init__(self, folder: Path):
        self.folder = folder

    def __call__(self, task: tuple[str, str]) -> None:
        title, body = task
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"proposals-{title.rsplit(' ', 1)[-1]}.md"
        path.write_text(f"# {title}\n\n{body}")
        log(f"task written to {path}")


class VikunjaSink:
    """Create the task with PUT /api/v1/projects/{id}/tasks (Vikunja's "Create a task").

    Vikunja stores descriptions as HTML from its TipTap editor, so checkboxes are sent as TipTap
    task items (`ul[data-type=taskList] > li[data-checked]`) to stay tickable in the UI.
    """

    def __init__(self, url: str, token: str, project_id: str, opener=urllib.request.urlopen):
        self.endpoint = f"{url.rstrip('/')}/api/v1/projects/{int(project_id)}/tasks"
        self.token, self.opener = token, opener

    def __call__(self, task: tuple[str, str]) -> None:
        title, body = task
        payload = json.dumps({"title": title, "description": markdown_to_html(body)}).encode()
        request = urllib.request.Request(self.endpoint, payload, method="PUT", headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with self.opener(request, timeout=30) as response:
            created = json.loads(response.read() or b"{}")
        log(f"task {created.get('id', '?')} created in the tracker")


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)


def markdown_to_html(body: str) -> str:
    """Just the markdown format_task writes: headings, checkboxes, bullets and paragraphs."""
    out, open_list = [], None
    for line in body.splitlines():
        kind = "task" if line.startswith("- [ ] ") else "bullet" if line.startswith("- ") else None
        if kind != open_list and open_list:
            out.append("</ul>")
        if kind and kind != open_list:
            out.append('<ul data-type="taskList">' if kind == "task" else "<ul>")
        open_list = kind
        if kind == "task":
            out.append('<li data-type="taskItem" data-checked="false"><label><input type="checkbox"><span></span>'
                       f"</label><div><p>{_inline(line[6:])}</p></div></li>")
        elif kind == "bullet":
            out.append(f"<li><p>{_inline(line[2:])}</p></li>")
        elif line.startswith("## "):
            out.append(f"<h3>{_inline(line[3:])}</h3>")
        elif line.strip():
            out.append(f"<p>{_inline(line)}</p>")
    if open_list:
        out.append("</ul>")
    return "".join(out)


def sink_from_env(env, state_dir: Path):
    """Vikunja when all three settings are present, else the file in the state dir."""
    url, token, project = (env.get(k, "") for k in ("VIKUNJA_URL", "VIKUNJA_TOKEN", "VIKUNJA_PROJECT_ID"))
    return VikunjaSink(url, token, project) if url and token and project else FileSink(state_dir)


# The run


def command_agent(prompt: str, env) -> tuple[int, str]:
    """Run DREAMER_CMD with the prompt on stdin, from the state dir so no project config loads."""
    command = shlex.split(env.get("DREAMER_CMD", ""))
    if not command:
        log("DREAMER_CMD is not set")
        return 2, ""
    cwd = Path(env.get("DREAMER_STATE_DIR") or DEFAULT_STATE).expanduser()
    try:
        result = subprocess.run(command, input=prompt, capture_output=True, text=True, cwd=cwd,
                                env={**os.environ, **env}, timeout=int(env.get("DREAMER_TIMEOUT", 3600)))
    except subprocess.TimeoutExpired:
        log("agent timed out")
        return 124, ""
    except OSError as error:
        log(f"agent did not start: {error}")
        return 127, ""
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode, result.stdout


def run(env, agent=command_agent, sink=None) -> int:
    """One dreaming run. The stored time moves only when the agent succeeded, its report parsed and
    the task was delivered, so a failed week is retried next week. It stores the run's start, so a
    change someone else makes during the run still triggers the next one."""
    vault = Path(env.get("DREAMER_VAULT") or env.get("CORTEX_VAULT") or "").expanduser()
    if not str(vault) or not vault.is_dir():
        log("set DREAMER_VAULT to the vault folder")
        return 2
    state_dir = Path(env.get("DREAMER_STATE_DIR") or DEFAULT_STATE).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)
    state = state_dir / "last-run"
    actor = env.get("DREAMER_ACTOR") or ACTOR
    last = parse_time(state.read_text().strip()) if state.exists() else None
    if not changed_since(read_changes(vault), last, actor):
        log("no vault change since the last run; skipping")
        return 0

    # Whole seconds, so a change logged in the same second as the start still counts as this run's.
    start = now().replace(microsecond=0)
    snapshot = take_snapshot(vault, state_dir)
    # The 30-day log rule needs a date the agent cannot get wrong.
    prompt = Path(env.get("DREAMER_PROMPT") or DEFAULT_PROMPT).read_text() + f"\nToday is {today()}.\n"
    code, output = agent(prompt, env)
    if code:
        log(f"agent exited {code}")
    entries = dreamer_entries(read_changes(vault), start, actor)
    limit = int(env.get("DREAMER_ENTITY_LIMIT") or ENTITY_LIMIT)
    restored, moves, failed, warnings = guard(vault, snapshot, entries, limit)
    # The snapshot is the only pre-run copy: keep it while any restore is still owed.
    if failed:
        log(f"{len(failed)} restore(s) failed; snapshot kept at {snapshot}")
    else:
        shutil.rmtree(snapshot, ignore_errors=True)

    report = parse_report(output) if code == 0 else None
    if code == 0 and report is None:
        log("agent output had no ```json report block")
    proposals, cap_warnings = enforce_caps(report or {"applied": [], "proposals": []}, len(entries))
    warnings += cap_warnings

    delivered = True
    if proposals or restored or moves or warnings or failed:
        task = format_task(today(), proposals, restored, moves, warnings, failed, snapshot)
        try:
            (sink or sink_from_env(env, state_dir))(task)
        except Exception as error:  # noqa: BLE001 - the tracker being down must not lose the proposals
            log(f"task sink failed ({error}); writing it to {state_dir}")
            FileSink(state_dir)(task)
            delivered = False

    ok = code == 0 and report is not None and delivered and not failed
    if ok:
        state.write_text(start.isoformat() + "\n")
    print(json.dumps({"ok": ok, "dreamer_changes": len(entries), "restored": restored, "restore_failed": failed,
                      "snapshot_kept": str(snapshot) if failed else None, "moves": moves,
                      "proposals": len(proposals), "warnings": warnings}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run(os.environ))
