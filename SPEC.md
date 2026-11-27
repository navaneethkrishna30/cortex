# Cortex vnext: spec

Decided 2026-11-27. The owner set the goal and delegated the design; the planning record (research, rejected options) is kept outside this public repo. This file is the single source for what Cortex should be and what is left to build.

## Goal

Cortex is the owner's **personal memory across chats**: the one MCP server every agent (Claude Code, opencode, Hermes, a future Jarvis) reads at session start and writes to when something worth keeping comes up. It must surface what matters across sessions and stay small. It is **not** a store for code, architecture or service operations: those live in each repo's `AGENTS.md`, `GLOSSARY.md`, `docs/adr/` and git history, and Cortex only points at them.

## Final state

### 1. What goes in the vault

| Folder | Holds | Limit |
|---|---|---|
| `core/` | rules, relation types, profile. Human-edited. | in every session's context |
| `memory/` | one-line facts about the owner: preferences, people, decisions and goals that span sessions | ≤ 300 chars of body; refused above |
| `entities/` | a short card per thing (person, place, device, service, repo, account, project): what it is, where its detail lives (repo URL, file), typed relations | ≤ 1200 chars of body; refused above, with "move detail to the repo" |
| `docs/` | longer non-code material with no repo home | none; not searched by default |
| `logs/` | append-only daily/debrief/raw | archived after 30 days |

Not in the vault: status narratives, deploy trails, version lines, how a codebase works. A card never repeats what its repo already says. No extra memory kinds (observation, page, mental model): the folders are the kinds. Entities may carry `kind:` (the values above).

### 2. Note states

- `status` is one of `current`, `superseded`, `expired`, `retired`. Writes with any other value are refused; free text (versions, phases) goes in its own field. Search hides everything except `current`.
- `expires: YYYY-MM-DD`: from that day the note leaves search and the memory index.
- Relation values resolve with or without `[[ ]]`, so `depends_on: homelab-common` still makes an edge.

### 3. Search

- BM25 (FTS5) + vector, fused by RRF, plus `exact=true`. No graph expansion beyond 1 hop, no derived semantic/temporal links (measured: noise).
- Each result carries `links` and `backlinks` (paths of notes it links to / is linked from), so the agent can follow a relation without another search.
- **Rerank with Voyage** (`rerank-3` by default, configurable) over the top 30 fused candidates when `VOYAGE_API_KEY` is set and reranking is enabled. Timeout → fall back to the RRF order, never fail a search. Snippets leave the server for this (accepted by the owner, 2026-11-27). A local reranker (fastembed `jinaai/jina-reranker-v1-turbo-en`) is the later option once the server has CPU to spare.
- Reranking is switched on only after it beats RRF on the eval set. Budget: whole search p50 < 2 s, p99 < 4 s; slower is acceptable if it is clearly better.
- No query date parsing or recency boost until the eval set shows misses they would fix.

### 4. Getting memory in and out of sessions

- Every tool carries `anthropic/alwaysLoad`. Server instructions inject core/ and the memory index; clients that drop them call `read("@context")`.
- First-prompt recall hook: one search on the first real prompt, top 5 added to context.
- Capture: a Stop hook fires **once per session**, after the 10th turn, telling the agent: "If this session produced a lasting fact about the owner, a decision or a preference, save at most 3 through write/update/supersede; code and service facts go to their repo; otherwise do nothing."
- Commands (Claude Code skills, mirrored for opencode): `/remember <fact>` (write or update, never a duplicate), `/forget <subject>` (find it, move to `.archive/`).

### 5. Dreaming (weekly upkeep)

A weekly headless agent run with its own `dreamer` token, working only through the Cortex tools, skipped when git shows no change since the last run.

- **Applies on its own:** fixes broken or bare links, archives `logs/` older than 30 days, sets `expires` where a note states an end date, shortens cards over the limit by replacing repo detail with the repo link.
- **Proposes only** (one task per run in the owner's task tracker): merges, supersedes, archiving live notes, moving content to a repo.
- Caps per run: 10 changes applied, 20 proposed. A change type moves from "propose" to "apply" after 30 decisions at ≥ 95% accepted.

### 6. Packaging and state

- Python. Installed from a git tag into `/opt/cortex`; all state in `/var/lib/cortex`: vault (git), `auth.sqlite`, `.cortex/index.sqlite`, model cache.
- The index rebuilds itself when its schema version changes. Backup copies the vault repo and `auth.sqlite`; index and model cache are rebuildable and excluded.
- Deployed and maintained by the `cortex` playbook of the server's infra repo (owner builds it).

## Built vs to build

| Piece | State |
|---|---|
| Vault, git history, typed relations, backlinks, supersede/archive, duplicate refusal | built (before vnext) |
| BM25 + vector RRF search, `exact`, docs/logs exclusion | built |
| `cortex eval <file>` (hit@k over a question TSV) | built, 10 sample questions |
| `anthropic/alwaysLoad` on every tool | built, deployed 2026-11-27 |
| `expires` (query-time, memory index, duplicate check) | built, deployed 2026-11-27 |
| First-prompt recall hook (Claude Code) | built, on the owner's laptop; copy in `clients/` (B8) |
| Vault cleanup to §1 | in progress |
| B1 status allow-list | built |
| B2 size limits | built |
| B3 bare relation values | built |
| B4 links in search results | built |
| B5 index schema version | built |
| B8 capture hook, recall hook, `/remember` `/forget` (`clients/`) | built, not installed |
| B9 dreaming (`clients/dreamer/`) | built, not installed |
| B6, B7 below | to build |

## Build tickets

Each ticket is sized for one agent session. Rules for every ticket: tests first (`uv run --frozen pytest`), all tests green before done, follow the existing code style (small modules, docstrings say why), no vault content or hostnames in commits (public repo), one commit per ticket on a branch off `vnext-spec`, ending with the agent's co-author line. Edit files with an editor tool, not scripted string replacement.

- **B1 Status allow-list.** `write`/`update`/`supersede` refuse a `status` outside `current|superseded|expired|retired` with a message naming the allowed values and suggesting a separate field. Search already hides non-current. Tests: refused write, refused update, allowed values pass, missing status still defaults to current.
- **B2 Size limits.** Refuse `memory/` bodies over 300 chars and `entities/` bodies over 1200 (body = after frontmatter, History section excluded) on write/update/supersede, with "move detail to the repo; link it". Limits in `Settings` (vault config can override). core/docs/logs exempt. Tests for each folder and the exemptions.
- **B3 Bare relation values.** A frontmatter relation value without `[[ ]]` (string or list) resolves like a link and makes an edge. Tests: bare string, bare list, mixed, unknown target stays `dst=None`.
- **B4 Links in search results.** Each search result (both RRF and exact paths) carries `links` and `backlinks` as lists of paths, from the `edges` table, < 1 ms. Update the `search` tool docstring. Tests.
- **B5 Index schema version.** Store `schema_version` in `meta`; on startup, if it differs from the code's, rebuild the index automatically (log it). Bump it in this ticket. Tests: old version triggers rebuild, same version doesn't.
- **B6 Voyage rerank.** `rerank.py`: Voyage rerank over the top 30 fused candidates (one snippet per note), config `rerank_provider` (`none` default | `voyage`), `rerank_model`, `rerank_timeout_s` (default 3), key from `VOYAGE_API_KEY`. Timeout or error → RRF order and a `reranked: false` flag in the response. Unit tests with a fake HTTP client; no network in tests.
- **B7 Eval harness.** Extend `cortex eval`: report hit@1, hit@8, MRR, p50/p95/p99 latency, per-question misses; `--rerank on|off` to compare; accept multiple expected paths per question (`path1|path2`). Keep the TSV format backward compatible. Tests on the sample vault. The owner supplies the real 50 questions.
- **B8 Capture and commands (client side).** In `clients/claude-code/`: the Stop hook script (once per session after turn 10, marker per session id, prints the capture instruction as a blocking reason), `/remember` and `/forget` skills, the first-prompt recall hook (copy of the owner's), and an install note. Mirror skills under `clients/opencode/`. Tests for the hook's once-per-session logic.
- **B9 Dreaming.** `clients/dreamer/`: the prompt (§5 rules, caps), a wrapper script (skip if no git change since last run; run a headless agent with only Cortex tools; after the run, revert any `dreamer` commit that removed body text; turn the agent's JSON proposals into one tracker task), and a systemd timer + service template. Server side: nothing new beyond a `dreamer` token. Tests for the skip logic, the revert check and the proposal formatting.

Owner steps (not for agents): the 50 eval questions with expected notes; the infra playbook (install from tag, `/etc/cortex.env` with `VOYAGE_API_KEY`, state dir, backup, timers); tagging releases.
