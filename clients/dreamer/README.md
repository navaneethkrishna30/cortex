# Cortex dreaming

A weekly headless agent run that tidies the vault (SPEC.md §5). It works only through the Cortex tools, with its own `dreamer` token, and is skipped when nobody has changed the vault since the last run. Nothing here is installed automatically.

| File | What it does |
|---|---|
| `prompt.md` | The agent's instructions: what it may fix on its own, what it may only propose, the caps, the JSON report it ends with |
| `dream.py` | The wrapper: skip check, snapshot, runs the agent, restores notes it cut, turns proposals into one task |
| `cortex-dreamer.service`, `cortex-dreamer.timer` | systemd templates: one run a week, caught up after downtime |

## What a run does

1. Reads the server's change log, `<vault>/.cortex/changes.jsonl` (one `{"at", "actor", "action", "paths"}` line per change). If no entry by anyone other than the dreamer is newer than the time stored after the last successful run (`last-run` in the state dir), it exits. A missing log means run.
2. Copies `memory/`, `entities/`, `docs/` and `logs/` to a snapshot folder in the state dir.
3. Runs `DREAMER_CMD` with `prompt.md` (plus today's date) on stdin, from the state dir.
4. Takes the change-log entries made during the run whose `actor` is the dreamer token's name (the server logs the token name for every change), and compares each file they name with the snapshot. A note is put back from the snapshot if it:
   - lost a body line (after the frontmatter, History left out) other than a change inside `[[ ]]`;
   - is gone, other than by a move.

   The one allowed cut: an `entities/` card over the limit, brought under it, keeping a repo link it already had, with every new line holding that link. A move of a `logs/` file to the same path under `.archive/` is fine; any other move is left in place and listed for you to review, since moving it back here would leave two copies. A `supersede` the agent made anyway is kept (it removes no text) and flagged as a warning. Restores are plain file writes, so the server picks them up as outside edits; one that lands on a note someone else also edited during the run overwrites that edit.
5. Deletes the snapshot. Reads the last ```` ```json ```` block of the agent's output, keeps at most 20 proposals, and warns if more than 10 changes were applied.
6. Creates one task, "Cortex dreaming proposals YYYY-MM-DD", with a checkbox per proposal plus what the guard restored, only when there is something in it.
7. Stores the run's start time only if the agent exited 0, its report parsed, and the task was delivered. Otherwise the next week runs again. If the tracker is down, the task is written to the state dir instead. A week with restores runs again the next week, because the restores are logged as outside edits.

For a `move` entry, `dream.py` doesn't rely on the order of `paths`: the source is the path that is gone and the target the one that is new; the rest are notes whose links the move rewrote. A moved note is compared with its pre-run source, so a move followed by a cut is caught. If a restore fails (for example, no write permission), the snapshot is kept, its path goes into the task under "Restore by hand", and the run counts as failed.

The run's summary goes to stdout as one JSON line; progress goes to stderr (both end up in the journal).

## Install

1. **Token.** In the Cortex UI, Access page, create a token named exactly `dreamer` with write access. The name is the `actor` the change log records and `dream.py` checks, so don't reuse a client's name. Cortex has only read and write scopes; a write token can call every write tool, `move` and `supersede` included. Archiving old logs needs `move` (Cortex allows a `logs/` file to move only into `.archive/`); supersedes and archiving live notes are left to proposals by the prompt, and backstopped by the check above.
2. **User.** Pick an unprivileged account that can read the vault, read `.cortex/changes.jsonl` and write the note folders (a restore writes a note back), for example a user in the vault's group with group write. Install and sign in the agent CLI as that user.
3. **Files.** Copy `dream.py` and `prompt.md` to an install dir. Write the MCP config and the env file below, both mode 600, owned by that user.
4. **systemd.** Copy the two unit files to `/etc/systemd/system/`, fill in every `<...>`, then `systemctl daemon-reload && systemctl enable --now cortex-dreamer.timer`. Try one run with `systemctl start cortex-dreamer.service` and `journalctl -u cortex-dreamer`.

### Claude Code as the agent

`mcp.json`:

```json
{
  "mcpServers": {
    "cortex": {
      "type": "http",
      "url": "https://<your-cortex-host>/mcp",
      "headers": { "Authorization": "Bearer ${CORTEX_DREAMER_TOKEN}" }
    }
  }
}
```

`cortex-dreamer.env`:

```bash
DREAMER_VAULT=/var/lib/cortex/vault
CORTEX_DREAMER_TOKEN=<the dreamer token>
ANTHROPIC_API_KEY=<key for the agent>
DREAMER_CMD='claude --bare -p --tools "" --strict-mcp-config --mcp-config <path-to>/mcp.json --allowedTools "mcp__cortex__*" --permission-mode dontAsk --permission-prompts none --no-session-persistence --max-turns 60 --output-format text'
```

Keep the single quotes around `DREAMER_CMD`: `dream.py` splits it like a shell, and `--tools ""` must stay an empty argument. What each flag does (checked against https://code.claude.com/docs/en/cli-reference, https://code.claude.com/docs/en/headless and https://code.claude.com/docs/en/permissions, 2026-11-27):

| Flag | Why |
|---|---|
| `-p`, `--output-format text` | non-interactive; stdout is the final reply, which holds the JSON report |
| `--bare` | skips the user's hooks, skills, plugins, CLAUDE.md and auto-discovered MCP servers. Bare mode never reads the subscription login, so it needs `ANTHROPIC_API_KEY`. Without `--bare`, a sign-in works but whatever is in that user's `~/.claude` loads too |
| `--tools ""` | no built-in tools (no Bash, no file edits); MCP tools are not affected by this flag |
| `--strict-mcp-config --mcp-config <file>` | only the Cortex server from that file |
| `--allowedTools "mcp__cortex__*"` | auto-approves every tool of the server named `cortex` |
| `--permission-mode dontAsk`, `--permission-prompts none` | anything not allowed is denied instead of waiting for a person |
| `--no-session-persistence`, `--max-turns 60` | no saved session; a runaway run stops |

`${CORTEX_DREAMER_TOKEN}` is expanded by Claude Code from the environment (`https://code.claude.com/docs/en/mcp`, "Environment variable expansion"). Any other agent works if it reads the prompt on stdin, uses only the Cortex tools under the `dreamer` token, and prints the report.

## Settings

| Variable | Default |
|---|---|
| `DREAMER_VAULT` | `CORTEX_VAULT`; one of them must point at the vault folder |
| `DREAMER_CMD` | none: the agent command, prompt on stdin |
| `DREAMER_STATE_DIR` | `~/.local/state/cortex-dreamer` (the unit sets `/var/lib/cortex-dreamer`) |
| `DREAMER_ACTOR` | `dreamer`: the token name whose changes are checked |
| `DREAMER_ENTITY_LIMIT` | `1200`: match the vault's `entity_body_limit` |
| `DREAMER_TIMEOUT` | `3600` seconds |
| `DREAMER_PROMPT` | `prompt.md` beside `dream.py` |
| `VIKUNJA_URL`, `VIKUNJA_TOKEN`, `VIKUNJA_PROJECT_ID` | unset. With all three, the task goes to Vikunja (`https://<vikunja-host>`, an API token that can create tasks, the project's numeric id) through `PUT /api/v1/projects/{id}/tasks` ("Create a task" in Vikunja's API, https://vikunja.io/docs/ and its OpenAPI at `/api/v1/docs.json`). Without them, the task is written to `proposals-YYYY-MM-DD.md` in the state dir |

## Reviewing proposals

Open the week's task (or the file in the state dir). Each checkbox names a kind, the notes and a reason:

- **merge** / **supersede**: in a normal session, read the notes and use `supersede` (or `/remember` with the merged fact).
- **archive**: `/forget <subject>`, or `move` the note to `.archive/`.
- **move-to-repo**: put the detail in the repo's docs, then shorten the card to the repo link.
- **fix-link**: pick the right target and edit the link.

Tick what you did and close the task. "Restored" lists notes the guard already put back, for your information; "Moves to review" lists moves to keep or undo with `move`. Once a kind of proposal has had 30 decisions at 95% or more accepted, move it from "Propose only" to "Apply on your own" in `prompt.md` (SPEC §5).
