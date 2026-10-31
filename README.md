# Cortex

A local-first memory layer for AI agents: a folder of markdown notes plus a SQLite index, both in one git repo, served through one MCP server. Every connected agent shares the same rules, facts and history.

## Run it

```bash
git clone https://github.com/navaneethkrishna30/cortex-vault-template ~/cortex-memory
uv tool install .
CORTEX_VAULT=~/cortex-memory cortex serve
```

The first run prints a setup token. Open `http://127.0.0.1:8766/ui`, set a password and enrol TOTP, then create a token or approve an OAuth login.

## Ports

| Port | Serves | Expose with |
|---|---|---|
| 8765 | `/mcp`, `/oauth/*` | Tailscale Funnel or Cloudflare Tunnel |
| 8766 | `/ui` | `tailscale serve` only, never a public tunnel |

Set `CORTEX_PUBLIC_URL` to the public address clients use. `deploy/` has the systemd unit and install script.

## Commands

- `cortex serve`: run both listeners
- `cortex index [--rebuild]`: sync the index, or re-embed everything after a model change
- `cortex doctor`: index size, repo size, remotes, broken links
- `cortex eval FILE`: score search against a TSV of question and expected path
- `cortex auth reset`: start UI setup again, for a lost password or TOTP device

## Develop

```bash
uv run pytest
uv run cortex --vault "$(mktemp -d)" doctor
```

`tests/eval` holds a small search eval; copy its vault somewhere temporary before running it.
