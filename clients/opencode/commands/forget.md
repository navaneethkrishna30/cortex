---
description: Forget a Cortex note by archiving it to .archive/ after you confirm which note it is; never deletes
---

Forget this in Cortex: $ARGUMENTS

Use the Cortex MCP tools (`search`, `read`, `move`).

1. `search` for the subject. If nothing matches, say so and stop.
2. Show the user the matching note paths with a one-line summary each, and ask which to forget. Do not move anything until the user confirms a specific note. If several match, they may pick more than one.
3. For each confirmed note, `move(path, ".archive/<path>")`, keeping its folder: `memory/foo.md` goes to `.archive/memory/foo.md`. Never delete a note; archiving keeps it in git history and out of search.
4. `move` returns `linked_from`: the live notes that still link to the archived one. List them to the user and offer to remove or fix those links.
5. If the fact is outdated rather than unwanted, suggest `/remember` with the new fact instead: superseding keeps the history of what changed.
