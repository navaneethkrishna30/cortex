---
name: remember
description: Save a lasting fact about the owner (a preference, a person, a decision, a goal) to Cortex memory, updating the existing note instead of making a duplicate. Use when the user says /remember or asks you to remember something across sessions.
argument-hint: "[fact]"
---

Remember this in Cortex: $ARGUMENTS

Use the Cortex MCP tools (`search`, `read`, `write`, `update`, `supersede`).

1. **Is it a Cortex fact?** Cortex holds facts about the owner that span sessions. How a codebase or a service works belongs in that repo (`AGENTS.md`, `docs/`, an ADR), not in Cortex: say so, offer to put it there, and stop. If the repo has no home for it, at most add the repo link to its card in `entities/`.
2. **Search first.** `search` for the subject in a few words. If a live note already covers it, `read` it to get its hash.
3. **Change the existing note, never add a second one:**
   - Same fact, only a frontmatter field changes (status, a date, a relation): `update(path, fields, reason, expected_hash)`.
   - The fact itself changed: `supersede(old=path, new="<the new one-line fact>", reason)`.
   - Same fact, better wording: `write(path, content, expected_hash)`, where `content` is the whole note as `read` returned it (frontmatter included) with only the line changed.
4. **No match:** `write("memory/<short-slug>.md", "<the fact>")`. A memory is one sentence on one line, at most 300 characters. Link detail instead of copying it: `[[entities/...]]` for a card, the repo URL for code.
5. If Cortex refuses the write (a duplicate, a stale hash, too long), act on its message: it names the note to edit or what to cut.
6. Tell the user in one line which note you created or changed.
