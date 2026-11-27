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


# B4: links in search results


def linked_vault(store):
    seed_targets(store)
    store.write("entities/app.md", card("app", "depends_on: server\nlocated_in: [[server]]\n") + "Runs beside [[laptop]] and [[app]].\n", actor="a")
    store.write("memory/likes-app.md", "Nav likes the [[app]] zebra.", actor="a")


def result_for(found, path):
    return next(r for r in found["results"] if r["path"] == path)


def test_search_results_carry_links_and_backlinks(store):
    from cortex.search import search

    linked_vault(store)
    app = result_for(search(store.index, "app"), "entities/app.md")
    assert app["links"] == ["entities/laptop.md", "entities/server.md"]
    assert app["backlinks"] == ["memory/likes-app.md"]
    server = result_for(search(store.index, "server"), "entities/server.md")
    assert server["links"] == [] and server["backlinks"] == ["entities/app.md"]


def test_exact_results_carry_links_and_backlinks(store):
    from cortex.search import search

    linked_vault(store)
    found = search(store.index, "zebra", exact=True)
    likes = result_for(found, "memory/likes-app.md")
    assert likes["links"] == ["entities/app.md"] and likes["backlinks"] == []
