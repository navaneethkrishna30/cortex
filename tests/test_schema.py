import logging

import pytest

from cortex.embed import HashEmbedder
from cortex.index import SCHEMA_VERSION, Index

JUNK = ("ghost.md", "links_to", "nowhere", None)


def leave_junk(store, version):
    """An index from older code: a stale edge row, and an old or missing schema version."""
    db = store.index.db
    db.execute("INSERT INTO edges VALUES(?,?,?,?)", JUNK)
    if version is None:
        db.execute("DELETE FROM meta WHERE key='schema_version'")
    else:
        db.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', ?)", (version,))
    db.commit()


def has_junk(index):
    return index.db.execute("SELECT 1 FROM edges WHERE src=?", (JUNK[0],)).fetchone() is not None


def test_fresh_index_is_stamped_with_the_schema_version(store):
    assert store.index._meta("schema_version") == SCHEMA_VERSION


@pytest.mark.parametrize("old", [None, "1"])
def test_old_schema_version_triggers_a_rebuild(store, caplog, old):
    store.write("entities/app.md", "# App\nThe app.\n", actor="a")
    leave_junk(store, old)
    with caplog.at_level(logging.INFO, logger="cortex.index"):
        index = Index(store.vault, HashEmbedder())
    assert not has_junk(index)
    assert index._meta("schema_version") == SCHEMA_VERSION
    assert index.db.execute("SELECT 1 FROM files WHERE path='entities/app.md'").fetchone()
    assert "rebuild" in caplog.text


def test_same_schema_version_does_not_rebuild(store, caplog):
    leave_junk(store, SCHEMA_VERSION)
    with caplog.at_level(logging.INFO, logger="cortex.index"):
        index = Index(store.vault, HashEmbedder())
    assert has_junk(index)
    assert "rebuild" not in caplog.text
