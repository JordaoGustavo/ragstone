from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ragtone.chunking import first_sentence
from ragtone.emoji import emojize

_SLACK_TS = re.compile(r"^\d+\.\d+$")
_SLACK_ID = re.compile(r"^[CGD][A-Za-z0-9]{8,}$", re.I)
_EXCERPT_PREFIX = re.compile(r"^[\s:.\-–—/]+")


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
    seen_stamp: str = ""
    seen_count: int | None = None
    unread: bool = False


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


def _id_like_title(title: str) -> bool:
    return bool(_SLACK_TS.fullmatch(title) or _SLACK_ID.fullmatch(title))


def _placeholder_title(hit: dict[str, Any], title: str) -> bool:
    if not title or _id_like_title(title):
        return True
    channel = str(hit.get("channel_or_space") or "").strip()
    if title == channel:
        return True
    if str(hit.get("source") or "chat") != "chat":
        return False
    aliases = {
        str(hit.get("native_id") or "").strip(),
        str(hit.get("thread_id") or "").strip(),
        str(hit.get("ref") or "").strip(),
    }
    return title in {item for item in aliases if item}


def title_from_hit(hit: dict[str, Any], fallback: str = "") -> str:
    title = str(hit.get("title") or "").strip()
    if not _placeholder_title(hit, title):
        return title
    sentence = first_sentence(str(hit.get("text") or ""))
    if sentence:
        return sentence
    return title or fallback or "sem título"


def excerpt_from_hit(hit: dict[str, Any], title: str) -> str:
    text = re.sub(r"\s+", " ", str(hit.get("text") or "")).strip()
    if not text:
        return ""
    if text == title:
        return ""
    if text.startswith(title):
        rest = _EXCERPT_PREFIX.sub("", text[len(title) :]).strip()
        if rest:
            text = rest
    return text[:800]


def present_title(node: dict[str, Any]) -> str:
    title = str(node.get("title") or "").strip()
    if str(node.get("source") or "") != "chat":
        return title
    aliases = {
        str(node.get("native_id") or "").strip(),
        str(node.get("thread_id") or "").strip(),
        str(node.get("ref") or "").strip(),
    }
    if not _id_like_title(title) and title not in {item for item in aliases if item}:
        return title
    sentence = first_sentence(str(node.get("excerpt") or ""))
    return sentence or title


def present_board(board: Board) -> dict[str, Any]:
    data = board.model_dump()
    for node in data["nodes"]:
        node["title"] = emojize(present_title(node))
        node["excerpt"] = emojize(node["excerpt"])
    return data


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
    title = title_from_hit(hit, ref)
    return BoardNode(
        id=_uid("n"),
        source=source,
        ref=ref,
        title=title,
        excerpt=excerpt_from_hit(hit, title),
        url=str(hit.get("url") or ""),
        native_id=str(hit.get("native_id") or ""),
        thread_id=str(hit.get("thread_id") or ""),
        parent_id=str(hit.get("parent_id") or ""),
        x=x,
        y=y,
        seen_stamp=_stamp(hit.get("updated_at")),
        seen_count=None,
        unread=False,
    )


def entity_key(node: BoardNode) -> tuple[str, str] | None:
    if node.source == "jira":
        ref = node.parent_id or node.native_id or node.ref
        return ("issue", ref) if ref else None
    if node.source == "confluence":
        ref = node.parent_id or node.ref
        return ("page", ref) if ref else None
    ref = node.thread_id or node.ref
    if not ref or str(ref).startswith("local:"):
        return None
    return ("thread", ref)


def _stamp(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def fingerprint(hits: list[dict[str, Any]]) -> tuple[str, int]:
    stamp = ""
    for hit in hits:
        value = _stamp(hit.get("updated_at"))
        if value > stamp:
            stamp = value
    return stamp, len(hits)


def capture_seen(node: BoardNode, hits: list[dict[str, Any]]) -> BoardNode:
    stamp, count = fingerprint(hits)
    if hits:
        node.seen_stamp = stamp
        node.seen_count = count
    node.unread = False
    return node


def apply_unreads(
    board: Board,
    lookup: Callable[[BoardNode], list[dict[str, Any]]],
) -> bool:
    changed = False
    for node in board.nodes:
        if entity_key(node) is None:
            if node.unread:
                node.unread = False
                changed = True
            continue
        hits = lookup(node)
        if not hits:
            continue
        stamp, count = fingerprint(hits)
        if not node.seen_stamp and node.seen_count is None:
            node.seen_stamp = stamp
            node.seen_count = count
            node.unread = False
            changed = True
            continue
        unread = stamp > (node.seen_stamp or "") or (
            node.seen_count is not None and count > node.seen_count
        )
        if node.seen_count is None and not unread:
            node.seen_count = count
            changed = True
        if node.unread != unread:
            node.unread = unread
            changed = True
    return changed


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
