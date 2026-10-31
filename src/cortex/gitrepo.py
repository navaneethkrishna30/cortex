"""Thin wrapper around the git CLI: every vault change becomes one attributed commit."""

from __future__ import annotations

import subprocess
from pathlib import Path

HUMAN = "human"


class Git:
    def __init__(self, root: Path):
        self.root = root

    def run(self, *args: str, check: bool = True) -> str:
        result = subprocess.run(
            ["git", *args], cwd=self.root, check=check, capture_output=True, text=True
        )
        return result.stdout

    def init(self) -> None:
        if not (self.root / ".git").exists():
            self.run("init", "-q", "-b", "main")

    def commit(self, message: str, actor: str, paths: list[str]) -> str | None:
        """Commit just `paths` as `actor`. Returns the sha, or None if nothing changed."""
        self.run("add", "-A", "--", *paths)
        if not self.run("diff", "--cached", "--name-only", "--", *paths).strip():
            return None
        email = f"{actor.replace(' ', '-')}@cortex.local"
        self.run(
            "-c", f"user.name={actor}", "-c", f"user.email={email}",
            "commit", "-q", "-m", f"{message}\n\nCortex-Actor: {actor}", "--", *paths,
        )
        return self.run("rev-parse", "HEAD").strip()

    def history(self, path: str, limit: int = 50) -> list[dict]:
        """Commits touching `path`, newest first, following renames."""
        out = self.run("log", "--follow", f"-{limit}", "--format=%H%x1f%ct%x1f%an%x1f%s", "--", path, check=False)
        rows = (line.split("\x1f") for line in out.splitlines())
        return [{"sha": sha, "at": float(at), "author": author, "subject": subject} for sha, at, author, subject in rows]

    def show(self, sha: str, path: str) -> str | None:
        """`path` as it stood at `sha`, or None if it wasn't there under that name."""
        result = subprocess.run(["git", "show", f"{sha}:{path}"], cwd=self.root, capture_output=True, text=True)
        return result.stdout if result.returncode == 0 else None

    def dirty_paths(self) -> list[str]:
        output = self.run("status", "--porcelain", "-z", "--untracked-files=all")
        entries = output.split("\0")
        paths, skip = [], False
        for entry in entries:
            if skip:
                paths.append(entry)
                skip = False
            elif entry:
                paths.append(entry[3:])
                skip = entry[0] == "R"
        return [path for path in paths if path]

    def push(self) -> list[str]:
        """Push to every configured remote; returns the failures."""
        failures = []
        for remote in self.run("remote").split():
            result = subprocess.run(
                ["git", "push", "-q", remote, "HEAD"], cwd=self.root, capture_output=True, text=True
            )
            if result.returncode:
                failures.append(f"{remote}: {result.stderr.strip()}")
        return failures

    def size_bytes(self) -> int:
        return sum(f.stat().st_size for f in (self.root / ".git").rglob("*") if f.is_file())
