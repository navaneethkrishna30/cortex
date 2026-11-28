# Coding standards

Rules a reviewer checks every change against. `make check` enforces the ones marked **[auto]** (ruff, pyright, pytest, `tests/test_ui_style.py`); the rest are review rules. Terms follow `GLOSSARY.md`; decisions not to re-open are in `docs/adr/`.

## Shape
- Small modules whose docstrings say why, not what. A module earns its place by hiding behaviour behind a small interface; a pass-through gets folded into its caller.
- `Store` is the one way to change the vault: it checks, writes, reindexes and appends to the change log under its lock. Nothing else writes notes.
- The index holds only what can be rebuilt from the vault (ADR 0001). A new table or column bumps `SCHEMA_VERSION`.
- Configuration lives in `Settings` (`config.py`), from the vault's `.cortex/config.toml` or `CORTEX_*` variables. No other module reads the environment, apart from the secret `VOYAGE_API_KEY`.

## Failure
- Voyage (embedding, reranking) never fails a write or a search (ADR 0003). Its errors are caught at the call site with `# noqa: BLE001 - <why>`, logged once, and the caller carries on without vectors or keeps the fused order.
- Network calls happen outside `Index.lock`, so a slow API never holds up searches. A write's inline embed still runs under `Store.lock`, bounded by the short timeout; add no other network call under it. Calls made while someone waits use the short timeout; only the backfill is patient.
- Refusals are `VaultError` with a message that tells the agent what to do next. They reach the agent as tool errors, never as a 500.
- Startup needs no network.

## Tests
- Tests first for a behaviour change; all green before done. **[auto]**
- Tests go through a module's interface (`Store`, `search`, the MCP and HTTP apps) with `HashEmbedder` and fake HTTP clients. No network in tests.
- UI: tokens only, no inline styles or raw colours outside `tokens.css` (DESIGN.md). **[auto]**

## Style
- `ruff check` and `ruff format` clean, pyright basic clean. **[auto]**
- A `noqa` or `pyright: ignore` names the rule and gives the reason on the same line.

## Repo and commits
- The repo is public: no hostnames, tailnet names, vault content, tokens or keys in code, tests, commits or PRs.
- One change per branch, merged by PR; nothing is pushed to `main` directly. Commit messages say what changed and why.
