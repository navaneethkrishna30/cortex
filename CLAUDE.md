# cortex

The owner's personal memory across chats: a markdown vault, a rebuildable SQLite index and one MCP server, with a small web UI. Python, uv.

- Vocabulary: `GLOSSARY.md`. Use its terms in code, commits and PRs.
- Before proposing structure (a new store, a different embedder, git in the vault, merging the listeners), read `docs/adr/`. Those choices were made on purpose.
- What Cortex should be and what is left to build: `SPEC.md`. UI rules: `DESIGN.md`.
- Before finishing any change, `make check` passes (ruff, pyright, pytest) and the change meets `codingstandards.md`. `make fix` formats.
- Setup: `uv sync --group dev`. The repo is public: no hostnames, vault content or tokens anywhere in it.
- Deployed as a container by a separate infra repo, pinned to a commit; merging here changes nothing that runs until that pin moves.
