import pytest

from cortex.config import Settings
from cortex.embed import HashEmbedder
from cortex.index import Index
from cortex.store import Store
from cortex.vault import Vault

RELATIONS = """# Relations
| Relation | Meaning |
|---|---|
| `part_of` | belongs to a larger thing |
| `depends_on` | needs another thing |
| `located_in` | lives on something |
"""


@pytest.fixture
def settings(tmp_path):
    vault = tmp_path / "vault"
    (vault / "core").mkdir(parents=True)
    (vault / "core/relations.md").write_text(RELATIONS)
    (vault / "core/rules.md").write_text("# Rules\nOne fact, one place.\n")
    return Settings(vault=vault.resolve(), auth_db=tmp_path / "auth.sqlite", quiet_seconds=0.1)


@pytest.fixture
def store(settings):
    vault = Vault(settings)
    index = Index(vault, HashEmbedder())
    index.sync()
    return Store(vault, index)
