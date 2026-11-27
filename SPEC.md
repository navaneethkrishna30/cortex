# Cortex vnext: spec

Decided 2026-11-27. Nav set the goal and delegated the design; the planning record (research, rejected options) is kept outside this public repo.

## Goal

Cortex is the owner's **personal memory across chats**: the one MCP server every agent (Claude Code, opencode, Hermes, a future Jarvis) reads at session start and writes to when something worth keeping comes up. It must surface what matters across sessions and stay small. It is **not** a store for code, architecture or service operations: those live in each repo's `AGENTS.md`, `GLOSSARY.md`, `docs/adr/` and git history, and Cortex only points at them.

## 1. What goes in the vault

| Folder | Holds | Limit |
|---|---|---|
| `core/` | rules, relation types, profile. Human-edited. | in every session's context |
| `memory/` | one-line facts about the owner: preferences, people, decisions and goals that span sessions | ≤ 300 chars; refused above |
| `entities/` | a short card per thing (person, place, device, service, repo, account, project): what it is, where its detail lives (repo URL, file), typed relations | ≤ 1200 chars; refused above, with "move detail to the repo" |
| `docs/` | longer non-code material with no repo home (interview notes, life admin, research about the owner's choices) | none; not searched by default |
| `logs/` | append-only daily/debrief/raw | archived after 30 days |

Not in the vault: status narratives, deploy trails, version lines, how a codebase works. A card never repeats what its repo already says.

No new memory kinds (observation, page, mental model): the folders above are the kinds. Entities may carry `kind:` (the values above) for filtering.

## 2. Note states

- `status` must be one of `current`, `superseded`, `expired`, `retired`. Writes with any other value are refused; free text (versions, phases) goes in its own field. Search hides everything except `current`.
- `expires: YYYY-MM-DD`: from that day the note leaves search and the memory index (shipped).
- Relation values resolve with or without `[[ ]]`, so `depends_on: homelab-common` still makes an edge.

## 3. Search

- Channels stay BM25 (FTS5) + vector, fused by RRF, plus `exact=true`. Measured on the real vault: 2-hop graph expansion returns 40–70% of the vault, semantic links at cosine 0.7 are ~70% noise, same-day links mean little. None are added.
- Each result carries the paths of the notes it links to and is linked from (1 hop, < 1 ms), so the agent can follow a relation without a second search.
- A reranker is added only if it beats the current ranking on the eval set and keeps a whole search under p50 2 s / p99 4 s on the server. Candidate: local fastembed `jinaai/jina-reranker-v1-turbo-en` (Apache-2.0, ~0.4 GB RSS), loaded at startup, reranking the top 30; measured ~0.5–0.9 s on a throttled laptop, to be re-timed on the server. Fallbacks: `jina-reranker-v1-tiny-en`, `ms-marco-MiniLM-L-6-v2`. Hosted rerankers are out: network latency alone is 0.4–1.3 s and note text would leave the server.
- No query date parsing or recency boost until the eval set shows misses they would fix.

## 4. Getting memory in and out of sessions

- Tools carry `anthropic/alwaysLoad` (shipped). Server instructions inject core/ and the memory index; clients that drop them call `read("@context")`.
- First-prompt recall hook: one search on the first real prompt, top 5 added to context (shipped for Claude Code).
- Capture: a Stop hook fires **once per session**, after the 10th turn, telling the agent: "If this session produced a lasting fact about the owner, a decision or a preference, save at most 3 through write/update/supersede; code and service facts go to their repo; otherwise do nothing."
- Commands (Claude Code skills, mirrored in opencode): `/remember <fact>` (write or update, never a duplicate), `/forget <subject>` (find it, move to `.archive/`).

## 5. Dreaming (weekly upkeep)

A weekly headless agent run with its own `dreamer` token, working only through the Cortex tools, skipped when git shows no change since the last run.

- **Applies on its own:** fixes broken or bare links, archives `logs/` older than 30 days, sets `expires` where a note states an end date, shortens cards over the limit by replacing repo detail with the repo link.
- **Proposes only** (one Vikunja task per run for the owner to tick): merges, supersedes, archiving live notes, moving content to a repo.
- Caps per run: 10 changes applied, 20 proposed. A change type moves from "propose" to "apply" after 30 decisions at ≥ 95% accepted.

## 6. Packaging and state

- Python as today. Install from a git tag into `/opt/cortex`; all state in `/var/lib/cortex`: vault (git), `auth.sqlite`, `.cortex/index.sqlite`, model cache.
- The index rebuilds itself when its schema version changes. Backup copies the vault repo and `auth.sqlite`; the index and model cache are rebuildable and excluded.
- Deploy is the `cortex` role of the server's infra repo.

## 7. Build order

1. Vault cleanup to section 1: shorten repo/service cards to links, archive code/ops docs whose content lives in a repo, fix free-text statuses and bare relations. Archive, never delete.
2. Server: status allow-list (2), size limits (1), bare relation values (2), 1-hop paths in results (3). Tests first.
3. Capture hook and `/remember` `/forget` (4).
4. Dreaming job and its Vikunja proposals (5).
5. Eval set (30 real questions) and baseline; reranker trial against it (3).
6. Tag a release; deploy through the infra role (6).
