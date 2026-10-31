"""Turn a note's markdown into the page the UI shows. Raw HTML in a note stays escaped text."""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import quote

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml

from .notes import field_links, parse

WIKILINK = re.compile(r"\[\[([^\]|#\n]+)(?:#[^\]|\n]*)?(?:\|([^\]\n]+))?\]\]")
TITLE = re.compile(r"^# .+\n?", re.MULTILINE)


def _wikilink_rule(state, silent: bool) -> bool:
    match = WIKILINK.match(state.src, state.pos)
    if not match:
        return False
    if not silent:
        token = state.push("wikilink", "", 0)
        token.meta = {"target": match.group(1).strip(), "label": match.group(2)}
    state.pos = match.end()
    return True


def _render_wikilink(self, tokens, index, options, env) -> str:
    return _link_html(tokens[index].meta["target"], tokens[index].meta["label"], env["resolve"])


def _link_html(target: str, label: str | None, resolve: Callable[[str], str | None]) -> str:
    text = escapeHtml(label or target)
    path = resolve(target)
    if not path:
        return f'<span class="wikilink broken" title="No note with this name">{text}</span>'
    return f'<a class="wikilink" href="/ui/files?path={quote(path, safe="")}">{text}</a>'


_markdown = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
_markdown.inline.ruler.before("link", "wikilink", _wikilink_rule)
_markdown.add_render_rule("wikilink", _render_wikilink)


def _field_html(value, resolve: Callable[[str], str | None]) -> str:
    links = field_links(value)
    if links:
        return " ".join(_link_html(target, None, resolve) for target in links)
    return escapeHtml(", ".join(map(str, value)) if isinstance(value, list) else str(value))


def render_note(text: str, path: str, resolve: Callable[[str], str | None]) -> dict:
    """The title, frontmatter fields and body HTML. The first `# heading` becomes the page title."""
    note = parse(text)
    title = note.title or path.rsplit("/", 1)[-1].removesuffix(".md")
    body = TITLE.sub("", note.body, count=1) if note.title else note.body
    fields = [(key, _field_html(value, resolve)) for key, value in note.fields.items()]
    return {"title": title, "fields": fields, "html": _markdown.render(body, {"resolve": resolve})}
