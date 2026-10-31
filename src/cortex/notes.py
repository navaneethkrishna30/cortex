"""The note format: frontmatter, [[links]], History sections, and memory lines."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

import yaml

LINK = re.compile(r"\[\[([^\]|#\n]+)(?:[#|][^\]\n]*)?\]\]")
FRONTMATTER = re.compile(r"\A---\n(.*?\n)?---\n?", re.DOTALL)
HISTORY = re.compile(r"^## History[ \t]*$", re.MULTILINE)
NEXT_HEADING = re.compile(r"^#{1,2} ", re.MULTILINE)
# Frontmatter keys Cortex itself owns; they never count as agent-chosen relations.
SYSTEM_LINK_KEYS = {"superseded_by"}


@dataclass
class Note:
    text: str
    fields: dict = field(default_factory=dict)
    frontmatter: str = ""
    body: str = ""

    @property
    def status(self) -> str:
        return str(self.fields.get("status", "current"))

    @property
    def title(self) -> str:
        match = re.search(r"^# (.+)$", self.body, re.MULTILINE)
        return match.group(1).strip() if match else ""

    @property
    def history(self) -> str:
        start, end = history_span(self.body)
        return self.body[start:end].strip() if start is not None else ""

    @property
    def body_without_history(self) -> str:
        start, end = history_span(self.body)
        return self.body if start is None else self.body[:start] + self.body[end:]


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def parse(text: str) -> Note:
    match = FRONTMATTER.match(text)
    if not match:
        return Note(text=text, body=text)
    raw = match.group(1) or ""
    try:
        fields = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        fields = {}
    if not isinstance(fields, dict):
        fields = {}
    return Note(text=text, fields=fields, frontmatter=raw, body=text[match.end() :])


def history_span(body: str) -> tuple[int | None, int | None]:
    match = HISTORY.search(body)
    if not match:
        return None, None
    following = NEXT_HEADING.search(body, match.end())
    return match.start(), following.start() if following else len(body)


def field_links(value) -> list[str]:
    """Link targets in a frontmatter value. YAML reads `[[x]]` as a nested list, so handle both."""
    if isinstance(value, str):
        return LINK.findall(value)
    if isinstance(value, list):
        if len(value) == 1 and isinstance(value[0], list) and len(value[0]) == 1:
            return [str(value[0][0])]
        return [target for item in value for target in field_links(item)]
    return []


def relation_edges(note: Note) -> list[tuple[str, str]]:
    """(relation, target) for each link held in a frontmatter field."""
    return [(key, target) for key, value in note.fields.items() for target in field_links(value)]


def body_links(note: Note) -> list[str]:
    return LINK.findall(note.body_without_history)


def format_value(value) -> str:
    """One frontmatter value as text. Links stay bare so the file reads like the spec's cards."""
    if isinstance(value, str) and (LINK.fullmatch(value) or re.fullmatch(r"[\w .:/@+-]+", value)):
        return value.strip()
    if isinstance(value, list) and all(isinstance(item, str) and LINK.fullmatch(item) for item in value):
        return "[" + ", ".join(f'"{item}"' for item in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def set_fields(text: str, changes: dict) -> str:
    """Edit frontmatter lines in place, leaving every other line byte-identical."""
    match = FRONTMATTER.match(text)
    lines = (match.group(1) or "").splitlines() if match else []
    body = text[match.end() :] if match else text
    for key, value in changes.items():
        rendered = f"{key}: {format_value(value)}"
        start = next((i for i, line in enumerate(lines) if line.startswith(f"{key}:")), None)
        if start is None:
            lines.append(rendered)
            continue
        end = start + 1
        while end < len(lines) and lines[end][:1] in (" ", "\t", "-"):
            end += 1
        lines[start:end] = [rendered]
    return "---\n" + "\n".join(lines) + "\n---\n" + body


def add_history(text: str, line: str) -> str:
    """Append to the History section, creating it at the end of the note when missing."""
    match = FRONTMATTER.match(text)
    head = text[: match.end()] if match else ""
    body = text[len(head) :]
    start, end = history_span(body)
    if start is None:
        return f"{head}{body.rstrip()}\n\n## History\n- {line}\n"
    section = body[start:end].rstrip()
    return f"{head}{body[:start]}{section}\n- {line}\n{body[end:]}"


def chunks(note: Note) -> list[tuple[str, str]]:
    """Split a note by heading into (heading, text), leaving History out of the index."""
    pieces: list[tuple[str, str]] = []
    heading, buffer = note.title, []
    in_fence = False
    for line in note.body_without_history.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        if not in_fence and re.match(r"#{1,3} ", line):
            if "".join(buffer).strip():
                pieces.append((heading, "\n".join(buffer).strip()))
            heading, buffer = line.lstrip("# ").strip(), [line]
        else:
            buffer.append(line)
    if "".join(buffer).strip():
        pieces.append((heading, "\n".join(buffer).strip()))
    return pieces
