# Embeddings come from Voyage, and search falls back to keywords when it is down

The owner decided on 2026-11-27 to embed with Voyage (`voyage-4-lite`, 1024 dimensions) instead of a local model, reversing the earlier local fastembed default: the container went from about 340 MiB to about 60 MiB. The price is that every note chunk and every query leaves the server, and that semantic search depends on a third party. So Voyage is never allowed to fail a write or a search: a write saves and is keyword-searchable at once, the note waits as pending and a backfill embeds it later; a search answers by keyword and says `semantic: false`. Calls made while someone waits get a short timeout and no retry; only the background backfill is patient. Startup needs no network. fastembed stays available as the `local` extra.

## Considered options

- Local fastembed model by default: no data leaves the server, but about 280 MiB more memory and a model download on first start. Revisit if the server gets memory to spare.
