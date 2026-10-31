"""Command line: cortex serve | index --rebuild | doctor | eval | auth reset."""

from __future__ import annotations

import argparse
import asyncio
import csv
from pathlib import Path

from .auth import AuthStore
from .config import load_settings
from .search import search
from .server import open_store, serve


def doctor(settings) -> None:
    store = open_store(settings)
    index_files = settings.state_dir.glob("index.sqlite*")
    print(f"vault:       {settings.vault}")
    print(f"embedder:    {store.index.embedder.name}")
    print(f"notes:       {len(store.vault.all_paths())}")
    print(f"index size:  {sum(f.stat().st_size for f in index_files) / 1e6:.1f} MB")
    print(f"repo size:   {store.git.size_bytes() / 1e6:.1f} MB")
    print(f"remotes:     {store.git.run('remote').split() or 'none: git is not a backup yet'}")
    broken = store.index.broken_links()
    print(f"broken links: {len(broken)}")
    for link in broken:
        print(f"  {link['src']} -> [[{link['target']}]]")


def run_eval(settings, file: str) -> None:
    """TSV of question<TAB>expected path. Prints hit@1, hit@3 and the misses."""
    store = open_store(settings)
    store.index.sync()
    rows = [r for r in csv.reader(Path(file).open(), delimiter="\t") if r and not r[0].startswith("#")]
    first = top3 = 0
    for question, expected in rows:
        paths = [r["path"] for r in search(store.index, question, "all")["results"]]
        first += paths[:1] == [expected]
        top3 += expected in paths[:3]
        if expected not in paths[:1]:
            print(f"miss: {question!r} wanted {expected}, got {paths[:3]}")
    print(f"hit@1 {first}/{len(rows)}  hit@3 {top3}/{len(rows)}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="cortex")
    parser.add_argument("--vault", help="vault folder (default: $CORTEX_VAULT)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the MCP and UI listeners")
    index_parser = commands.add_parser("index", help="sync the index with the folder")
    index_parser.add_argument("--rebuild", action="store_true", help="delete and re-embed everything")
    commands.add_parser("doctor", help="index size, repo size, broken links")
    eval_parser = commands.add_parser("eval", help="score search against a question file")
    eval_parser.add_argument("file")
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
        count = store.index.rebuild() if args.rebuild else store.index.sync()
        print(f"indexed {count} changed files")
    elif args.command == "doctor":
        doctor(settings)
    elif args.command == "eval":
        run_eval(settings, args.file)
    elif args.command == "auth":
        AuthStore(settings.auth_db).reset()
        print("Auth reset. Restart the server for a new setup token.")
