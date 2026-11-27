# Cortex clients

Hooks and commands that get memory into and out of agent sessions (SPEC.md §4). Nothing here is installed automatically: copy what you want.

| File | What it does |
|---|---|
| `claude-code/hooks/cortex-recall.py` | UserPromptSubmit hook: on the first real prompt, one Cortex `search`; the top 5 go into the context |
| `claude-code/hooks/cortex-capture.py` | Stop hook: once per session, after the 10th turn, asks the agent to save at most 3 lasting facts |
| `claude-code/skills/remember/SKILL.md` | `/remember <fact>`: search, then update or supersede the existing note, or write a new one |
| `claude-code/skills/forget/SKILL.md` | `/forget <subject>`: search, confirm with you, move the note to `.archive/` |
| `opencode/commands/*.md` | the same two commands for opencode |
| `opencode/AGENTS.snippet.md` | session-start and capture rules for opencode's `AGENTS.md` |
| `dreamer/` | weekly headless upkeep run (SPEC.md §5); own install steps in `dreamer/README.md` |

Both hooks need only Python 3 (standard library) and fail silent: if Cortex is down or a setting is missing, the session carries on as if they weren't there.

## Claude Code

1. Connect Cortex as an MCP server (the Access page in the UI gives the `claude mcp add` command) so the skills can call its tools.
2. Copy the files:

   ```bash
   mkdir -p ~/.claude/hooks ~/.claude/skills
   cp claude-code/hooks/cortex-*.py ~/.claude/hooks/
   chmod +x ~/.claude/hooks/cortex-*.py
   cp -r claude-code/skills/remember claude-code/skills/forget ~/.claude/skills/
   ```

3. Save a Cortex token for the recall hook (a read-only token is enough) to `~/.config/cortex/claude-code.token`, readable only by you (`chmod 600`).
4. Set the environment and add the hooks to `~/.claude/settings.json`:

   ```json
   {
     "env": {
       "CORTEX_URL": "https://<your-cortex-host>/mcp"
     },
     "hooks": {
       "UserPromptSubmit": [
         { "hooks": [{ "type": "command", "command": "~/.claude/hooks/cortex-recall.py", "timeout": 10 }] }
       ],
       "Stop": [
         { "hooks": [{ "type": "command", "command": "~/.claude/hooks/cortex-capture.py", "timeout": 5 }] }
       ]
     }
   }
   ```

   Stop and UserPromptSubmit take no `matcher`. Merge these into any hooks you already have rather than replacing them.

| Variable | Used by | Default |
|---|---|---|
| `CORTEX_URL` | recall hook | none: the hook does nothing until it is set |
| `CORTEX_TOKEN_FILE` | recall hook | `~/.config/cortex/claude-code.token` |

The capture hook needs no settings and never calls Cortex itself: it only tells the agent to, through the MCP tools. It keeps a turn count and a fired marker per session in `~/.cache/cortex-capture/`; the recall hook keeps a marker per session in `~/.cache/cortex-recall/`. Both are safe to delete.

## opencode

1. Add Cortex as a remote MCP server in your opencode config.
2. Copy the commands: `cp opencode/commands/*.md ~/.config/opencode/commands/` (or a project's `.opencode/commands/`).
3. Paste the section in `opencode/AGENTS.snippet.md` into `~/.config/opencode/AGENTS.md`. opencode gets no recall or capture hook from this folder, so those rules ask the agent to do both.
