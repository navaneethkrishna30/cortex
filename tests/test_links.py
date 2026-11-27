def card(name, frontmatter=""):
    return f"---\nstatus: current\n{frontmatter}---\n# {name.title()}\nThe {name}.\n"


def edges(store, path):
    return sorted((e["relation"], e["target"], e["dst"]) for e in store.index.links(path))


def seed_targets(store):
    store.write("entities/server.md", card("server"), actor="a")
    store.write("entities/laptop.md", card("laptop"), actor="a")


# B3: bare relation values


def test_bare_string_relation_makes_an_edge(store):
    seed_targets(store)
    store.write("entities/app.md", card("app", "depends_on: server\n"), actor="a")
    assert edges(store, "entities/app.md") == [("depends_on", "server", "entities/server.md")]
    assert store.read("entities/server.md")["backlinks"] == [{"src": "entities/app.md", "relation": "depends_on"}]


def test_bare_list_relation_makes_edges(store):
    seed_targets(store)
    store.write("entities/app.md", card("app", "depends_on: [server, laptop]\n"), actor="a")
    assert edges(store, "entities/app.md") == [
        ("depends_on", "laptop", "entities/laptop.md"),
        ("depends_on", "server", "entities/server.md"),
    ]


def test_mixed_bare_and_bracketed_values(store):
    seed_targets(store)
    store.write("entities/app.md", card("app", 'depends_on: ["[[server]]", laptop]\nlocated_in: [[[server]], entities/laptop]\n'), actor="a")
    assert edges(store, "entities/app.md") == [
        ("depends_on", "laptop", "entities/laptop.md"),
        ("depends_on", "server", "entities/server.md"),
        ("located_in", "entities/laptop", "entities/laptop.md"),
        ("located_in", "server", "entities/server.md"),
    ]


def test_unknown_bare_target_stays_unresolved(store):
    store.write("entities/app.md", card("app", "depends_on: nowhere\n"), actor="a")
    assert edges(store, "entities/app.md") == [("depends_on", "nowhere", None)]


def test_bare_values_of_other_fields_are_not_edges(store):
    seed_targets(store)
    store.write("entities/app.md", card("app", "port: 8080\nowner: server\nkind: service\n"), actor="a")
    assert edges(store, "entities/app.md") == []
