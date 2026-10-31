"""Safe access to the vault folder: path checks, reads, link resolution."""

from __future__ import annotations

import os
from pathlib import Path

from .config import Settings
from .notes import Note, parse, sha256

ARCHIVE = ".archive"


class VaultError(ValueError):
    """A request the vault refuses. The message goes back to the agent, so it says what to do."""


class Vault:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.vault

    def clean(self, path: str, *, must_exist: bool = False, archive_ok: bool = False) -> str:
        """Normalise a vault-relative note path, refusing anything outside the allowed folders."""
        if not path or "\x00" in path or path.startswith(("/", "\\")) or "\\" in path:
            raise VaultError(f"invalid path: {path!r}")
        parts = Path(path).parts
        if ".." in parts or any(part.startswith(".") and part != ARCHIVE for part in parts):
            raise VaultError(f"path not allowed: {path}")
        allowed = list(self.settings.allowed_folders) + ([ARCHIVE] if archive_ok else [])
        if not parts or parts[0] not in allowed:
            raise VaultError(f"path must be inside one of {allowed}: {path}")
        clean = "/".join(parts)
        if not clean.endswith(".md"):
            clean += ".md"
        full = self.root / clean
        # A symlink inside the vault must not lead outside it.
        if not full.resolve().is_relative_to(self.root):
            raise VaultError(f"path escapes the vault: {path}")
        if must_exist and not full.is_file():
            raise VaultError(f"no such note: {clean}")
        return clean

    def clean_folder(self, folder: str) -> str:
        if folder in ("", ".", "/"):
            return ""
        probe = self.clean(folder.rstrip("/") + "/x.md")
        return probe[: -len("/x.md")]

    def read_text(self, path: str) -> str:
        return (self.root / path).read_text(encoding="utf-8")

    def write_text(self, path: str, text: str) -> None:
        full = self.root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        temp = full.with_name(f".{full.name}.tmp")
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, full)

    def load(self, path: str) -> Note:
        return parse(self.read_text(path))

    def hash(self, path: str) -> str:
        return sha256(self.read_text(path))

    def all_paths(self, folders: list[str] | None = None) -> list[str]:
        found = []
        for folder in folders or self.settings.allowed_folders:
            base = self.root / folder
            if not base.is_dir():
                continue
            for file in base.rglob("*.md"):
                relative = file.relative_to(self.root).as_posix()
                if not any(part.startswith(".") for part in Path(relative).parts):
                    found.append(relative)
        return sorted(found)

    def resolve_link(self, target: str, paths: list[str] | None = None) -> str | None:
        """[[docs/plan]] is a path; [[cortex]] matches by file name, entities first."""
        target = target.strip().removesuffix(".md")
        paths = self.all_paths() if paths is None else paths
        if f"{target}.md" in paths or (self.root / f"{target}.md").is_file():
            return f"{target}.md"
        stem = target.rsplit("/", 1)[-1].lower()
        matches = [p for p in paths if p.rsplit("/", 1)[-1][:-3].lower() == stem]
        for folder in ("entities/", "memory/", "docs/", "core/"):
            for match in matches:
                if match.startswith(folder):
                    return match
        return matches[0] if matches else None

    def relation_types(self) -> set[str]:
        """Relation names from the table in core/relations.md."""
        file = self.root / "core/relations.md"
        if not file.is_file():
            return set()
        types = set()
        for line in file.read_text().splitlines():
            cell = line.split("|")[1].strip().strip("`") if line.startswith("|") else ""
            if cell and cell.replace("_", "").isalnum() and cell.lower() != "relation":
                types.add(cell)
        return types
