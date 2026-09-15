from pathlib import Path

from starlette.testclient import TestClient

from ragtone.board import (
    BoardNode,
    BoardStore,
    apply_unreads,
    capture_seen,
    delete_walk,
    empty_board,
    link_nodes,
    new_walk,
    pin_node,
    present_board,
    unlink_edge,
    unpin_node,
)
from ragtone.board_http import create_app
from ragtone.chunking import chat_chunk, confluence_chunks, jira_chunks
from ragtone.embeddings import HashEmbedder
from ragtone.memory_index import InMemoryIndex
from ragtone.retrieval import RetrievalService
from ragtone.settings import Settings


def test_pinning_a_slack_message_uses_the_text_as_title() -> None:
    board = empty_board()
    pin_node(
        board,
        {
            "source": "chat",
            "title": "C024BE7LT",
            "channel_or_space": "C024BE7LT",
            "text": "SSO caiu no gateway",
            "thread_id": "1710000000.000100",
            "native_id": "1710000000.000100",
        },
    )
    node = board.nodes[0]
    assert node.title == "SSO caiu no gateway"
    assert node.excerpt == ""


def test_pinning_a_long_slack_message_keeps_the_rest_as_excerpt() -> None:
    board = empty_board()
    pin_node(
        board,
        {
            "source": "chat",
            "title": "C024BE7LT",
            "channel_or_space": "C024BE7LT",
            "text": (
                "SSO caiu no gateway de auth depois do deploy. "
                "O timeout aparece no load balancer da borda e ninguem consegue logar no console."
            ),
            "thread_id": "1710000000.000100",
            "native_id": "1710000000.000100",
        },
    )
    node = board.nodes[0]
    assert node.title == "SSO caiu no gateway de auth depois do deploy."
    assert node.excerpt.startswith("O timeout aparece")


def test_pinning_a_slack_message_without_title_does_not_use_the_timestamp() -> None:
    board = empty_board()
    pin_node(
        board,
        {
            "source": "chat",
            "text": "timeout no gateway",
            "thread_id": "1710000000.000100",
            "native_id": "1710000000.000100",
        },
    )
    assert board.nodes[0].title == "timeout no gateway"
    assert board.nodes[0].excerpt == ""


def test_present_board_rewrites_slack_timestamp_titles() -> None:
    board = empty_board()
    board.nodes.append(
        BoardNode(
            id="n1",
            source="chat",
            ref="1710000000.000100",
            title="1710000000.000100",
            excerpt="SSO caiu no gateway",
            native_id="1710000000.000100",
            thread_id="1710000000.000100",
        )
    )
    view = present_board(board)
    assert view["nodes"][0]["title"] == "SSO caiu no gateway"
    assert board.nodes[0].title == "1710000000.000100"


def test_pinning_drops_a_card_without_a_rope() -> None:
    board = empty_board()
    pin_node(
        board,
        {
            "source": "chat",
            "title": "SSO caiu",
            "text": "timeout no gateway",
            "thread_id": "t1",
            "native_id": "1.0",
        },
    )
    pin_node(
        board,
        {
            "source": "jira",
            "title": "ABC-9",
            "text": "investigar SSO",
            "native_id": "ABC-9",
            "parent_id": "ABC-9",
        },
    )
    assert len(board.nodes) == 2
    assert board.edges == []
    assert board.walk().node_ids == []


def test_same_thread_is_not_pinned_twice() -> None:
    board = empty_board()
    hit = {"source": "jira", "title": "ABC-1", "native_id": "ABC-1", "parent_id": "ABC-1"}
    pin_node(board, hit)
    pin_node(board, hit)
    assert len(board.nodes) == 1


def test_explicit_link_extends_the_active_walk() -> None:
    board = empty_board()
    pin_node(board, {"source": "chat", "title": "A", "thread_id": "a"})
    pin_node(board, {"source": "chat", "title": "B", "thread_id": "b"})
    first, second = board.nodes
    link_nodes(board, first.id, second.id)
    assert board.walk().node_ids == [first.id, second.id]
    assert board.edges[0].from_id == first.id


def test_unlinking_cuts_the_rope_and_keeps_the_cards() -> None:
    board = empty_board()
    pin_node(board, {"source": "chat", "title": "A", "thread_id": "a"})
    pin_node(board, {"source": "chat", "title": "B", "thread_id": "b"})
    pin_node(board, {"source": "chat", "title": "C", "thread_id": "c"})
    first, second, third = board.nodes
    link_nodes(board, first.id, second.id)
    link_nodes(board, second.id, third.id)
    unlink_edge(board, board.edges[0].id)
    assert len(board.edges) == 1
    assert board.edges[0].from_id == second.id
    assert board.edges[0].to_id == third.id
    assert board.walk().node_ids == [second.id, third.id]
    assert {node.id for node in board.nodes} == {first.id, second.id, third.id}


def test_unpinning_a_card_drops_its_ropes() -> None:
    board = empty_board()
    pin_node(board, {"source": "chat", "title": "A", "thread_id": "a"})
    pin_node(board, {"source": "chat", "title": "B", "thread_id": "b"})
    pin_node(board, {"source": "chat", "title": "C", "thread_id": "c"})
    first, second, third = board.nodes
    link_nodes(board, first.id, second.id)
    link_nodes(board, second.id, third.id)
    unpin_node(board, second.id)
    assert [node.title for node in board.nodes] == ["A", "C"]
    assert board.edges == []
    assert board.walk().node_ids == []


def test_board_store_round_trip(tmp_path: Path) -> None:
    store = BoardStore(tmp_path / "board.json")
    board = empty_board()
    pin_node(board, {"source": "confluence", "title": "Runbook", "id": "99"})
    store.save(board)
    loaded = store.load()
    assert loaded.nodes[0].title == "Runbook"
    assert loaded.active_walk_id == board.active_walk_id


def test_board_http_persists_pin(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    first = client.post(
        "/api/board/pin",
        json={"hit": {"source": "jira", "title": "VPN", "native_id": "NET-1", "parent_id": "NET-1"}},
    )
    assert first.status_code == 200
    assert first.json()["nodes"][0]["title"] == "VPN"
    again = client.get("/api/board")
    assert again.json()["nodes"][0]["title"] == "VPN"
    assert again.json()["edges"] == []


def test_board_http_pins_slack_text_as_title(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    pinned = client.post(
        "/api/board/pin",
        json={
            "hit": {
                "source": "chat",
                "title": "C024BE7LT",
                "channel_or_space": "C024BE7LT",
                "text": "SSO caiu no gateway",
                "thread_id": "1710000000.000100",
            }
        },
    )
    assert pinned.json()["nodes"][0]["title"] == "SSO caiu no gateway"


def test_board_http_renders_emoji_shortcodes_on_cards(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    pinned = client.post(
        "/api/board/pin",
        json={
            "hit": {
                "source": "chat",
                "title": "see :thread:",
                "text": ":+1: ok",
                "thread_id": "a",
            }
        },
    )
    node = pinned.json()["nodes"][0]
    assert node["title"] == "see 🧵"
    assert node["excerpt"] == "👍 ok"
    again = client.get("/api/board").json()["nodes"][0]
    assert again["title"] == "see 🧵"
    assert again["excerpt"] == "👍 ok"


def test_board_http_unlinks_an_edge(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    client.post(
        "/api/board/pin",
        json={"hit": {"source": "chat", "title": "A", "thread_id": "a"}},
    )
    client.post(
        "/api/board/pin",
        json={"hit": {"source": "chat", "title": "B", "thread_id": "b"}},
    )
    nodes = client.get("/api/board").json()["nodes"]
    linked = client.post(
        "/api/board/link",
        json={"from_id": nodes[0]["id"], "to_id": nodes[1]["id"]},
    )
    edge_id = linked.json()["edges"][0]["id"]
    cut = client.post("/api/board/unlink", json={"id": edge_id})
    assert cut.status_code == 200
    body = cut.json()
    assert body["edges"] == []
    assert len(body["nodes"]) == 2


def test_board_http_unpins_a_card_and_its_rope(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    client.post("/api/board/pin", json={"hit": {"source": "chat", "title": "A", "thread_id": "a"}})
    client.post("/api/board/pin", json={"hit": {"source": "chat", "title": "B", "thread_id": "b"}})
    nodes = client.get("/api/board").json()["nodes"]
    client.post("/api/board/link", json={"from_id": nodes[0]["id"], "to_id": nodes[1]["id"]})
    gone = client.post("/api/board/unpin", json={"id": nodes[0]["id"]})
    assert gone.status_code == 200
    body = gone.json()
    assert [node["title"] for node in body["nodes"]] == ["B"]
    assert body["edges"] == []


def test_deleting_a_walk_keeps_the_cards() -> None:
    board = empty_board()
    pin_node(board, {"source": "chat", "title": "A", "thread_id": "a"})
    pin_node(board, {"source": "chat", "title": "B", "thread_id": "b"})
    first, second = board.nodes
    link_nodes(board, first.id, second.id)
    first_walk = board.walks[0].id
    new_walk(board, "Trilha 2")
    delete_walk(board, first_walk)
    assert [walk.name for walk in board.walks] == ["Trilha 2"]
    assert [node.title for node in board.nodes] == ["A", "B"]
    assert board.edges == []


def test_board_page_opens_a_finder_for_index_hits(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    page = client.get("/").text
    assert 'id="open-finder"' in page
    assert 'id="open-finder-float"' in page
    assert 'id="finder"' in page
    assert 'id="finder-hits"' in page
    assert 'id="q"' in page
    css = client.get("/static/board.css").text
    assert ".finder-hit" in css
    assert "body.finder-open" in css
    js = client.get("/static/board.js").text
    assert "openFinder" in js
    assert "summonFinder" in js
    assert "hitPreview" in js
    assert "browseIndex" in js
    assert 'id="finder-filters"' in page
    assert ".finder-filters" in css
    assert 'id="hide-rail"' in page
    assert 'id="show-rail"' in page
    assert "body.rail-collapsed" in css
    assert "ragtone.rail-collapsed" in js
    assert ".node-badge" in css
    assert "node.unread" in js
    assert "/api/board/seen" in js
    assert "refreshUnreads" in js
    assert "item.url" in js
    empty = RetrievalService(InMemoryIndex(), HashEmbedder(8))
    client = TestClient(create_app(settings, retrieval=empty))
    data = client.get("/api/recent").json()
    assert data["ok"] is True
    assert data["hits"] == []


def test_admin_page_opens_recents_per_connector(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    page = client.get("/").text
    assert 'id="admin-recent"' in page
    assert 'id="start-backfill"' in page
    assert 'id="admin-backfill-days"' in page
    assert 'id="admin-backfill-connectors"' in page
    assert 'id="admin-backfill-sources"' in page
    assert 'id="stop-sync"' in page
    js = client.get("/static/board.js").text
    assert "start-backfill" in js
    assert "backfill_days" in js
    assert "fillBackfillConnectors" in js
    assert "fillBackfillSources" in js
    assert "backfill-source" in js
    assert "/api/admin/sync/stop" in js
    assert "toggleAdminRecent" in js
    assert "admin-open" in js
    assert "/api/recent?source=" in js
    assert "lookAtChat" in js
    assert "lookAtWatch" in js
    assert "Primeiro corte" in js
    assert "/api/admin/peek" in js
    css = client.get("/static/board.css").text
    assert ".admin-recent-source" in css
    assert ".admin-tools" in css
    assert ".admin-sheet" in css
    assert ".admin-backfill-connectors" in css
    assert ".admin-backfill-connectors[hidden]" in css
    assert ".watch-peek" in css
    assert ".watch-presets" in css
    admin = client.get("/api/admin").json()
    assert admin["recent"] == []
    assert [row["name"] for row in admin["connectors"]] == ["jira", "confluence", "chat"]


def test_board_http_deletes_a_walk(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    client.post("/api/board/walk", json={"name": "Extra"})
    walks = client.get("/api/board").json()["walks"]
    assert len(walks) == 2
    gone = client.post("/api/board/walk/delete", json={"id": walks[-1]["id"]})
    assert gone.status_code == 200
    assert [walk["name"] for walk in gone.json()["walks"]] == ["Trilha 1"]


def test_thread_reply_after_pin_marks_the_card_unread() -> None:
    board = empty_board()
    pin_node(
        board,
        {
            "source": "chat",
            "title": "SSO caiu",
            "thread_id": "t1",
            "updated_at": "1.0",
        },
    )
    node = board.nodes[0]
    capture_seen(node, [{"updated_at": "1.0"}])
    apply_unreads(board, lambda _: [{"updated_at": "1.0"}, {"updated_at": "1.1"}])
    assert node.unread is True


def test_confluence_edit_marks_the_card_unread() -> None:
    board = empty_board()
    pin_node(board, {"source": "confluence", "title": "Runbook", "id": "99", "updated_at": "2026-01-01"})
    node = board.nodes[0]
    capture_seen(node, [{"updated_at": "2026-01-01"}])
    apply_unreads(board, lambda _: [{"updated_at": "2026-02-01"}])
    assert node.unread is True


def test_jira_comment_count_marks_the_card_unread() -> None:
    board = empty_board()
    pin_node(
        board,
        {
            "source": "jira",
            "title": "ABC-9",
            "native_id": "ABC-9",
            "parent_id": "ABC-9",
            "updated_at": "2026-01-01",
        },
    )
    node = board.nodes[0]
    capture_seen(node, [{"updated_at": "2026-01-01"}])
    apply_unreads(
        board,
        lambda _: [{"updated_at": "2026-01-01"}, {"updated_at": "2026-01-01T12:00:00"}],
    )
    assert node.unread is True


def test_first_index_sighting_does_not_badge() -> None:
    board = empty_board()
    pin_node(board, {"source": "confluence", "title": "Runbook", "id": "99"})
    node = board.nodes[0]
    apply_unreads(board, lambda _: [{"updated_at": "2026-01-01"}])
    assert node.unread is False
    assert node.seen_stamp == "2026-01-01"
    assert node.seen_count == 1


def test_local_note_is_never_unread() -> None:
    board = empty_board()
    pin_node(board, {"source": "chat", "title": "nota", "native_id": "local:1", "id": "local:1"})
    called = False

    def lookup(_node):
        nonlocal called
        called = True
        return [{"updated_at": "9.9"}]

    apply_unreads(board, lookup)
    assert called is False
    assert board.nodes[0].unread is False


def _app_with_index(tmp_path: Path, chunks):
    embedder = HashEmbedder(8)
    store = InMemoryIndex()
    store.upsert(chunks, embedder.embed([chunk.text for chunk in chunks]))
    retrieval = RetrievalService(store, embedder)
    settings = Settings(data_dir=tmp_path, embedder="hash")
    return TestClient(create_app(settings, retrieval=retrieval)), store, embedder


def test_board_http_search_returns_hits(tmp_path: Path) -> None:
    issue = jira_chunks(key="ABC-12", summary="SSO timeout", description="gateway")
    client, _, _ = _app_with_index(tmp_path, issue)
    blank = client.get("/api/search")
    assert blank.status_code == 200
    assert blank.json() == {"ok": True, "hits": []}
    found = client.get("/api/search", params={"q": "ABC-12"})
    assert found.status_code == 200
    body = found.json()
    assert body["ok"] is True
    assert body["hits"][0]["native_id"] == "ABC-12"


def test_board_http_search_returns_thread_parent(tmp_path: Path) -> None:
    docs = [
        chat_chunk(
            message_id="1.0",
            text="what broke?",
            channel="eng",
            thread_id="1.0",
            created_at="1.0",
        ),
        chat_chunk(
            message_id="1.1",
            text="the SSO gateway exploded",
            channel="eng",
            thread_id="1.0",
            created_at="1.1",
        ),
        *confluence_chunks(
            page_id="99",
            title="Runbook",
            body="## Symptoms\ntimeout\n\n## Fix\nrestart sso\n",
        ),
    ]
    client, _, _ = _app_with_index(tmp_path, docs)
    thread = client.get(
        "/api/search", params={"q": "SSO gateway", "source": "chat"}
    ).json()["hits"]
    assert [hit["native_id"] for hit in thread] == ["1.0"]
    page = client.get(
        "/api/search", params={"q": "restart sso", "source": "confluence"}
    ).json()["hits"]
    assert [hit["native_id"] for hit in page] == ["99"]
    assert page[0]["title"] == "Runbook"


def test_board_http_search_opens_jira_from_cloud_id(tmp_path: Path) -> None:
    docs = [
        *jira_chunks(key="ABC-12", summary="SSO timeout", description="gateway"),
        *confluence_chunks(page_id="99", title="Runbook", body="restart sso", space="ENG"),
        chat_chunk(
            message_id="1710000000.000100",
            text="sso caiu",
            channel="C024BE7LT",
            thread_id="1710000000.000100",
        ),
    ]
    embedder = HashEmbedder(8)
    store = InMemoryIndex()
    store.upsert(docs, embedder.embed([chunk.text for chunk in docs]))
    settings = Settings(
        data_dir=tmp_path,
        embedder="hash",
        atlassian_cloud_id="https://example.atlassian.net",
        slack_workspace="https://example.slack.com",
    )
    retrieval = RetrievalService(store, embedder)
    client = TestClient(create_app(settings, retrieval=retrieval))
    jira = client.get("/api/search", params={"q": "ABC-12"}).json()["hits"][0]
    assert jira["url"] == "https://example.atlassian.net/browse/ABC-12"
    page = client.get("/api/expand", params={"kind": "page", "ref": "99"}).json()["items"][0]
    assert page["url"] == "https://example.atlassian.net/wiki/spaces/ENG/pages/99"
    thread = client.get(
        "/api/expand", params={"kind": "thread", "ref": "1710000000.000100"}
    ).json()["items"][0]
    assert thread["url"] == "https://example.slack.com/archives/C024BE7LT/p1710000000000100"


def test_board_http_search_does_not_embed(tmp_path: Path) -> None:
    class _BoomEmbedder:
        dims = 8

        def embed(self, texts):
            raise AssertionError("spotlight must not embed")

    docs = [
        chat_chunk(
            message_id="1.0",
            text="what broke?",
            channel="eng",
            thread_id="1.0",
            created_at="1.0",
        ),
        chat_chunk(
            message_id="1.1",
            text="the SSO gateway exploded",
            channel="eng",
            thread_id="1.0",
            created_at="1.1",
        ),
    ]
    store = InMemoryIndex()
    store.upsert(docs, HashEmbedder(8).embed([chunk.text for chunk in docs]))
    retrieval = RetrievalService(store, _BoomEmbedder())
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings, retrieval=retrieval))
    thread = client.get(
        "/api/search", params={"q": "SSO gateway", "source": "chat"}
    ).json()["hits"]
    assert [hit["native_id"] for hit in thread] == ["1.0"]


def test_board_http_search_does_not_500_when_index_rejects(tmp_path: Path) -> None:
    class _BoomStore:
        def lexical_search(self, *args, **kwargs):
            raise RuntimeError("current license is non-compliant for [Reciprocal Rank Fusion (RRF)]")

    settings = Settings(data_dir=tmp_path, embedder="hash")
    retrieval = RetrievalService(_BoomStore(), HashEmbedder(8))
    client = TestClient(create_app(settings, retrieval=retrieval))
    response = client.get("/api/search", params={"q": "SSO"})
    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "search failed", "hits": []}


def test_board_http_badges_a_new_slack_reply(tmp_path: Path) -> None:
    root = chat_chunk(
        message_id="1.0",
        text="sso caiu",
        channel="eng",
        thread_id="1.0",
        created_at="1.0",
    )
    client, store, embedder = _app_with_index(tmp_path, [root])
    pinned = client.post(
        "/api/board/pin",
        json={"hit": {"source": "chat", "title": "eng", "thread_id": "1.0", "updated_at": "1.0"}},
    )
    assert pinned.json()["nodes"][0]["unread"] is False
    reply = chat_chunk(
        message_id="1.1",
        text="gateway timeout",
        channel="eng",
        thread_id="1.0",
        created_at="1.1",
    )
    store.upsert([reply], embedder.embed([reply.text]))
    board = client.get("/api/board").json()
    assert board["nodes"][0]["unread"] is True
    seen = client.post("/api/board/seen", json={"id": board["nodes"][0]["id"]})
    assert seen.status_code == 200
    assert seen.json()["nodes"][0]["unread"] is False


def test_board_http_badges_confluence_and_jira_updates(tmp_path: Path) -> None:
    page = confluence_chunks(
        page_id="99",
        title="Runbook",
        body="passo um",
        updated_at="2026-01-01",
    )
    issue = jira_chunks(key="ABC-9", summary="VPN", description="túnel", updated_at="2026-01-01")
    client, store, embedder = _app_with_index(tmp_path, page + issue)
    client.post(
        "/api/board/pin",
        json={"hit": {"source": "confluence", "title": "Runbook", "parent_id": "99", "updated_at": "2026-01-01"}},
    )
    client.post(
        "/api/board/pin",
        json={
            "hit": {
                "source": "jira",
                "title": "ABC-9",
                "native_id": "ABC-9",
                "parent_id": "ABC-9",
                "updated_at": "2026-01-01",
            }
        },
    )
    later_page = confluence_chunks(
        page_id="99",
        title="Runbook",
        body="passo um\n\npasso dois",
        updated_at="2026-02-01",
    )
    later_issue = jira_chunks(
        key="ABC-9",
        summary="VPN",
        description="túnel",
        updated_at="2026-02-01",
        comments=[{"id": "c1", "body": "ainda cai", "updated_at": "2026-02-01"}],
    )
    store.upsert(later_page + later_issue, embedder.embed([c.text for c in later_page + later_issue]))
    nodes = {node["source"]: node for node in client.get("/api/board").json()["nodes"]}
    assert nodes["confluence"]["unread"] is True
    assert nodes["jira"]["unread"] is True


def test_board_http_seen_missing_card(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    missing = client.post("/api/board/seen", json={"id": "nope"})
    assert missing.status_code == 404
