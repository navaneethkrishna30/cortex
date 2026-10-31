"""Design tokens stay in one file: see DESIGN.md."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1] / "src/cortex"
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def test_no_inline_styles_or_raw_colours():
    for file in (ROOT / "templates").glob("*.html"):
        text = file.read_text()
        assert not re.search(r"\bstyle=|<style", text), file.name
        assert not HEX.search(re.sub(r"#activity", "", text)), file.name
    assert not HEX.search((ROOT / "static/ui.css").read_text())
    assert not HEX.search((ROOT / "static/ui.js").read_text())
