<!-- Copy the section below into ~/.config/opencode/AGENTS.md (all projects) or a project's
AGENTS.md. opencode has no first-prompt or Stop hook here, so these rules stand in for the
Claude Code recall and capture hooks. -->

## Cortex memory

- At the start of every session, before your first answer, call the Cortex `read` tool with `@context` unless Cortex's rules and memory index are already in your context. Then `search` Cortex once for the topic of the first real request.
- Near the end of a long session (10 or more turns), once: if it produced a lasting fact about the owner, a decision or a preference, save at most 3 through Cortex write/update/supersede; code and service facts go to their repo; otherwise do nothing.
- `/remember <fact>` and `/forget <subject>` are the commands for saving and archiving by hand.
