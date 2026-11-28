# Cortex

The owner's personal memory across chats: one place every agent reads at the start of a session and writes to when something worth keeping comes up.

## People and clients

**Owner**:
The one person whose memory the vault holds, and the only human who edits it.
_Avoid_: user, admin

**Client**:
An agent or app that reaches Cortex with its own token or OAuth grant, and whose name goes on every change it makes.
_Avoid_: app, integration, consumer

**Actor**:
Who made a change: a client's name, `human` for the owner, or `dreamer`.
_Avoid_: author, user

**Read token / write token**:
A client's credential. A read token sees only the tools that change nothing.
_Avoid_: API key, scope token

## The vault

**Vault**:
The folder of markdown notes that is Cortex's memory and its source of truth.
_Avoid_: database, repo, store

**Note**:
One markdown file in the vault, with optional frontmatter.
_Avoid_: document, file, page, record

**Core**:
The rules, relation types and profile, edited only by the owner and given to every session.
_Avoid_: system prompt, config

**Memory line**:
A note in `memory/` holding one short fact about the owner: a preference, a person, a decision or a goal.
_Avoid_: memory item, fact note, observation

**Card**:
A short note in `entities/` about one thing (a person, place, device, service, repo, account, project): what it is, where its detail lives, and its relations.
_Avoid_: entity note, profile, page

**Doc**:
A longer note in `docs/` with no repo to live in. Not searched unless asked.
_Avoid_: page, article

**Log**:
An append-only daily, debrief or raw note in `logs/`.
_Avoid_: journal, diary

**Memory index**:
The list of current memory lines that every session receives with core.
_Avoid_: summary, digest

**Context**:
What a session starts with: core plus the memory index.
_Avoid_: preamble, bootstrap

## Note states

**Status**:
Whether a note still counts: `current`, `superseded`, `expired` or `retired`. Only current notes surface.
_Avoid_: state, phase, version

**Expires**:
The date from which a note's fact stops being true and the note leaves search and the memory index.
_Avoid_: TTL, deadline

**Supersede**:
Retire a note in favour of the note that replaces it, keeping the old one in its history.
_Avoid_: replace, overwrite, deprecate

**Archive**:
Where forgotten notes go (`.archive/`). Nothing is ever deleted.
_Avoid_: delete, trash, bin

**History**:
The section at the end of a note that says what changed in it and why.
_Avoid_: changelog (that is the vault-wide change log), revisions

**Change log**:
The vault-wide, append-only record of every change: when, by which actor, what action, which notes.
_Avoid_: audit log, git history

**Outside edit**:
A change made to the vault's files directly, not through Cortex. Logged as the owner's.
_Avoid_: manual edit, external change

## Links

**Relation**:
A typed link from one note to another in frontmatter (`part_of`, `depends_on`, `located_in`, `uses`, `owned_by`, `replaces`). The note that moves owns it.
_Avoid_: edge, reference, association

**Link**:
A `[[note]]` in a note's body or a relation value, pointing at another note.
_Avoid_: wikilink, reference

**Backlink**:
A link seen from the note it points to.
_Avoid_: inbound link, reverse link

**Broken link**:
A link whose target no note answers to.
_Avoid_: dead link, dangling reference

## Finding notes

**Search**:
Finding current memory lines and cards for a query, best first, reporting what was left out and why.
_Avoid_: query, lookup, retrieval

**Keyword match**:
A note found because it shares words with the query.
_Avoid_: lexical hit, BM25 hit

**Semantic match**:
A note found because it is close in meaning to the query. Missing while the embedder is down; search then says it is not semantic.
_Avoid_: vector hit, similarity hit

**Rerank**:
A second opinion that reorders the best matches by relevance. Off until it beats the plain order on the eval set.
_Avoid_: re-score, re-sort

**Embedder**:
What turns text into meaning for semantic matches. Voyage in production.
_Avoid_: model, encoder, vectoriser

**Pending note**:
A saved note that keyword search already finds but semantic search does not yet, because the embedder hasn't answered.
_Avoid_: unindexed, queued note

**Backfill**:
Giving pending notes their meaning later, once the embedder answers.
_Avoid_: reindex, catch-up

**Duplicate**:
A new note on a subject a current note already covers. Refused, with the existing note returned.
_Avoid_: copy, clone

**Eval set**:
The owner's questions with the notes that should answer them, used to score search.
_Avoid_: benchmark, test set

## Upkeep

**Dreaming**:
The weekly upkeep run by the `dreamer` client: it applies small fixes and proposes the rest to the owner.
_Avoid_: cleanup job, maintenance, cron

**Proposal**:
A change the dreamer wants but may not apply on its own (a merge, a supersede, an archive); the owner decides.
_Avoid_: suggestion, recommendation
