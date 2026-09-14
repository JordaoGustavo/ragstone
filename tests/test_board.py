from pathlib import Path

from starlette.testclient import TestClient

from ragtone.board import (
    BoardStore,
    delete_walk,
    empty_board,
    link_nodes,
    new_walk,
    pin_node,
    unlink_edge,
    unpin_node,
)
from ragtone.board_http import create_app
from ragtone.settings import Settings


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


def test_board_http_recent_is_quiet_when_index_is_down(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    data = client.get("/api/recent").json()
    assert data["ok"] is False
    assert data["hits"] == []


def test_board_http_deletes_a_walk(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    client.post("/api/board/walk", json={"name": "Extra"})
    walks = client.get("/api/board").json()["walks"]
    assert len(walks) == 2
    gone = client.post("/api/board/walk/delete", json={"id": walks[-1]["id"]})
    assert gone.status_code == 200
    assert [walk["name"] for walk in gone.json()["walks"]] == ["Trilha 1"]
