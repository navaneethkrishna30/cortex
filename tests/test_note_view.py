"""Notes are written by agents too, so what they hold must not run in the UI."""

from cortex.note_view import render_note


def render(text: str) -> str:
    return render_note(text, "memory/x.md", lambda target: "memory/real.md" if target == "real" else None)["html"]


def test_raw_html_and_script_links_stay_inert():
    html = render("<script>alert(1)</script>\n\n[bad](javascript:alert(1))\n\n[[<img src=x onerror=alert(1)>]]\n")
    assert "<script" not in html and "<img" not in html and 'href="javascript' not in html


def test_wikilinks_resolve_and_broken_ones_show():
    html = render("[[real|the note]] and [[missing]] and `[[real]]`\n")
    assert 'href="/ui/files?path=memory%2Freal.md">the note</a>' in html
    assert 'class="wikilink broken"' in html
    assert "<code>[[real]]</code>" in html
