"""Command line: cortex serve | index --rebuild | doctor | eval | auth reset."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

from . import evaluation
from .auth import AuthStore
from .config import load_settings
from .server import open_store, serve


def vault_bytes(root: Path) -> int:
    """The notes, without Cortex's own state (reported apart) or a leftover .git."""
    files = (f for f in root.rglob("*") if f.is_file())
    return sum(f.stat().st_size for f in files if f.relative_to(root).parts[0] not in (".cortex", ".git"))


def doctor(settings) -> None:
    store = open_store(settings)
    index_files = settings.state_dir.glob("index.sqlite*")
    print(f"vault:       {settings.vault}")
    print(f"embedder:    {store.index.embedder.name}")
    print(f"notes:       {len(store.vault.all_paths())}")
    print(f"index size:  {sum(f.stat().st_size for f in index_files) / 1e6:.1f} MB")
    print(f"vault size:  {vault_bytes(settings.vault) / 1e6:.1f} MB")
    print(f"change log:  {store.changes.count()} entries")
    broken = store.index.broken_links()
    print(f"broken links: {len(broken)}")
    for link in broken:
        print(f"  {link['src']} -> [[{link['target']}]]")


def run_eval(settings, file: str, rerank: str | None = None, as_json: bool = False) -> None:
    """Score search on a question TSV. `rerank` on/off overrides the vault's setting for this run,
    so the owner can compare the two on the same questions."""
    if rerank is not None:
        settings = replace(settings, rerank_provider="voyage" if rerank == "on" else "none")
    store = open_store(settings)
    store.sync_outside_edits()
    report = evaluation.run(store.index, evaluation.load_questions(file), store.reranker)
    if rerank == "on" and report["reranked"] < report["questions"]:
        # Otherwise an "on" run that quietly fell back looks like a fair comparison.
        print(
            f"warning: {report['questions'] - report['reranked']} of {report['questions']} searches "
            "not reranked (check VOYAGE_API_KEY and the log)",
            file=sys.stderr,
        )
    print(json.dumps(report, indent=2) if as_json else evaluation.format_report(report))


def main() -> None:
    parser = argparse.ArgumentParser(prog="cortex")
    parser.add_argument("--vault", help="vault folder (default: $CORTEX_VAULT)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the MCP and UI listeners")
    index_parser = commands.add_parser("index", help="sync the index with the folder")
    index_parser.add_argument("--rebuild", action="store_true", help="delete and re-embed everything")
    commands.add_parser("doctor", help="index size, vault size, change log, broken links")
    eval_parser = commands.add_parser("eval", help="score search against a question file")
    eval_parser.add_argument("file", help="TSV: question<TAB>expected path, or path1|path2")
    eval_parser.add_argument("--rerank", choices=["on", "off"], help="force reranking on or off for this run")
    eval_parser.add_argument("--json", action="store_true", help="print the report as JSON")
    auth_parser = commands.add_parser("auth", help="recovery")
    auth_parser.add_argument("action", choices=["reset"])
    args = parser.parse_args()

    settings = load_settings(args.vault)
    if args.command == "serve":
        asyncio.run(serve(settings))
    elif args.command == "index":
        if args.rebuild:
            # A rebuild is how a model mismatch gets fixed, so it must not trip the check.
            settings.index_path.unlink(missing_ok=True)
        store = open_store(settings)
        # A plain sync goes through the store so edits made while the server was down get logged.
        changed = store.index.rebuild() if args.rebuild else store.sync_outside_edits()
        print(f"indexed {len(changed)} changed files")
    elif args.command == "doctor":
        doctor(settings)
    elif args.command == "eval":
        run_eval(settings, args.file, args.rerank, args.json)
    elif args.command == "auth":
        AuthStore(settings.auth_db).reset()
        print("Auth reset. Restart the server for a new setup token.")
