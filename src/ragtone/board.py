from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Camera(BaseModel):
    x: float = 80
    y: float = 64
    zoom: float = 1


class BoardNode(BaseModel):
    id: str
    source: str
    ref: str
    title: str
    excerpt: str = ""
    url: str = ""
    native_id: str = ""
    thread_id: str = ""
    parent_id: str = ""
    x: float = 0
    y: float = 0


class BoardEdge(BaseModel):
    id: str
    from_id: str
    to_id: str
    walk_id: str
    step: int


class Walk(BaseModel):
    id: str
    name: str = "Trilha 1"
    node_ids: list[str] = Field(default_factory=list)


class Board(BaseModel):
    camera: Camera = Field(default_factory=Camera)
    nodes: list[BoardNode] = Field(default_factory=list)
    edges: list[BoardEdge] = Field(default_factory=list)
    walks: list[Walk] = Field(default_factory=list)
    active_walk_id: str = ""

    def node(self, node_id: str) -> BoardNode | None:
        return next((item for item in self.nodes if item.id == node_id), None)

    def walk(self, walk_id: str | None = None) -> Walk:
        target = walk_id or self.active_walk_id
        for item in self.walks:
            if item.id == target:
                return item
        if not self.walks:
            created = Walk(id=_uid("w"), name="Trilha 1")
            self.walks.append(created)
            self.active_walk_id = created.id
            return created
        return self.walks[0]

    def find_ref(self, source: str, ref: str) -> BoardNode | None:
        return next(
            (item for item in self.nodes if item.source == source and item.ref == ref),
            None,
        )


def empty_board() -> Board:
    walk = Walk(id=_uid("w"), name="Trilha 1")
    return Board(walks=[walk], active_walk_id=walk.id)


def node_from_hit(hit: dict[str, Any], x: float, y: float) -> BoardNode:
    source = str(hit.get("source") or "chat")
    ref = str(
        hit.get("thread_id")
        or hit.get("parent_id")
        or hit.get("native_id")
        or hit.get("id")
        or _uid("ref")
    )
    return BoardNode(
        id=_uid("n"),
        source=source,
        ref=ref,
        title=str(hit.get("title") or ref),
        excerpt=str(hit.get("text") or "")[:800],
        url=str(hit.get("url") or ""),
        native_id=str(hit.get("native_id") or ""),
        thread_id=str(hit.get("thread_id") or ""),
        parent_id=str(hit.get("parent_id") or ""),
        x=x,
        y=y,
    )


def _slot(board: Board) -> tuple[float, float]:
    count = len(board.nodes)
    return 48 + (count % 3) * 340, 48 + (count // 3) * 220


def link_nodes(board: Board, from_id: str, to_id: str) -> Board:
    if from_id == to_id:
        return board
    if not board.node(from_id) or not board.node(to_id):
        raise KeyError("both nodes must be on the board")
    walk = board.walk()
    exists = any(
        edge.from_id == from_id and edge.to_id == to_id and edge.walk_id == walk.id
        for edge in board.edges
    )
    if not exists:
        step = sum(1 for edge in board.edges if edge.walk_id == walk.id) + 1
        board.edges.append(
            BoardEdge(
                id=_uid("e"),
                from_id=from_id,
                to_id=to_id,
                walk_id=walk.id,
                step=step,
            )
        )
    if from_id not in walk.node_ids:
        walk.node_ids.append(from_id)
    if not walk.node_ids or walk.node_ids[-1] != to_id:
        walk.node_ids.append(to_id)
    return board


def pin_node(
    board: Board,
    hit: dict[str, Any],
    *,
    x: float | None = None,
    y: float | None = None,
) -> Board:
    source = str(hit.get("source") or "chat")
    ref = str(
        hit.get("thread_id")
        or hit.get("parent_id")
        or hit.get("native_id")
        or hit.get("id")
        or ""
    )
    existing = board.find_ref(source, ref) if ref else None
    if existing:
        return board
    if x is None or y is None:
        x, y = _slot(board)
    board.nodes.append(node_from_hit(hit, x, y))
    return board


def new_walk(board: Board, name: str) -> Board:
    walk = Walk(id=_uid("w"), name=name or f"Trilha {len(board.walks) + 1}")
    board.walks.append(walk)
    board.active_walk_id = walk.id
    return board


def delete_walk(board: Board, walk_id: str) -> Board:
    if not any(item.id == walk_id for item in board.walks):
        raise KeyError(walk_id)
    board.edges = [item for item in board.edges if item.walk_id != walk_id]
    board.walks = [item for item in board.walks if item.id != walk_id]
    if not board.walks:
        created = Walk(id=_uid("w"), name="Trilha 1")
        board.walks.append(created)
        board.active_walk_id = created.id
    elif board.active_walk_id == walk_id:
        board.active_walk_id = board.walks[0].id
    return board


def unlink_edge(board: Board, edge_id: str) -> Board:
    edge = next((item for item in board.edges if item.id == edge_id), None)
    if edge is None:
        raise KeyError(edge_id)
    board.edges = [item for item in board.edges if item.id != edge_id]
    walk = next((item for item in board.walks if item.id == edge.walk_id), None)
    if walk is None:
        return board
    remaining = [item for item in board.edges if item.walk_id == walk.id]
    remaining.sort(key=lambda item: item.step)
    for step, item in enumerate(remaining, start=1):
        item.step = step
    ids: list[str] = []
    for item in remaining:
        if not ids:
            ids.append(item.from_id)
        elif ids[-1] != item.from_id:
            ids.append(item.from_id)
        ids.append(item.to_id)
    walk.node_ids = ids
    return board


def unpin_node(board: Board, node_id: str) -> Board:
    if board.node(node_id) is None:
        raise KeyError(node_id)
    for edge in list(board.edges):
        if edge.from_id == node_id or edge.to_id == node_id:
            unlink_edge(board, edge.id)
    board.nodes = [item for item in board.nodes if item.id != node_id]
    for walk in board.walks:
        walk.node_ids = [item for item in walk.node_ids if item != node_id]
    return board


def move_node(board: Board, node_id: str, x: float, y: float) -> Board:
    node = board.node(node_id)
    if node is None:
        raise KeyError(node_id)
    node.x = x
    node.y = y
    return board


class BoardStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Board:
        if not self.path.exists():
            board = empty_board()
            self.save(board)
            return board
        raw = json.loads(self.path.read_text())
        return Board.model_validate(raw)

    def save(self, board: Board) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(board.model_dump_json(indent=2) + "\n")
        tmp.replace(self.path)
