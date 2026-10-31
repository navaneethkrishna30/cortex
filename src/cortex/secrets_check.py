"""Refuse text that looks like a credential before it reaches git history."""

from __future__ import annotations

import re

PATTERNS = {
    "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY",
    "Anthropic/OpenAI key": r"\bsk-[A-Za-z0-9_-]{20,}",
    "GitHub token": r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
    "AWS key": r"\bAKIA[0-9A-Z]{16}\b",
    "Slack token": r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    "Google API key": r"\bAIza[0-9A-Za-z_-]{30,}",
    "Tailscale key": r"\btskey-[A-Za-z0-9-]{10,}",
    "JWT": r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.",
    "assigned secret": r"(?i)\b(api[_-]?key|secret|password|passwd|access[_-]?token)\b\s*[:=]\s*['\"]?[^\s'\"\]]{12,}",
}
COMPILED = {name: re.compile(pattern) for name, pattern in PATTERNS.items()}


def find_secret(text: str) -> str | None:
    for name, pattern in COMPILED.items():
        if pattern.search(text):
            return name
    return None
