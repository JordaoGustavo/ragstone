from __future__ import annotations

import re

from ragtone.models import Chunk

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def split_markdown_sections(text: str, fallback_title: str) -> list[tuple[str, str]]:
    current_title = fallback_title
    current: list[str] = []
    sections: list[tuple[str, str]] = []

    def flush() -> None:
        body = "\n".join(current).strip()
        if body:
            sections.append((current_title, body))

    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            flush()
            current_title = match.group(2).strip()
            current = []
        else:
            current.append(line)
    flush()
    if not sections:
        body = text.strip()
        if body:
            return [(fallback_title, body)]
        return []
    return sections


def confluence_chunks(
    *,
    page_id: str,
    title: str,
    body: str,
    url: str = "",
    space: str = "",
    updated_at: str | None = None,
    authors: tuple[str, ...] = (),
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for index, (heading, section) in enumerate(split_markdown_sections(body, title)):
        chunks.append(
            Chunk(
                source="confluence",
                native_id=f"{page_id}:{index}",
                text=section,
                title=heading,
                url=url,
                parent_id=page_id,
                channel_or_space=space,
                updated_at=updated_at,
                authors=authors,
            )
        )
    return chunks


def jira_chunks(
    *,
    key: str,
    summary: str,
    description: str,
    url: str = "",
    comments: list[dict[str, str]] | None = None,
    updated_at: str | None = None,
    authors: tuple[str, ...] = (),
) -> list[Chunk]:
    body = "\n\n".join(part for part in (summary, description) if part).strip()
    chunks = [
        Chunk(
            source="jira",
            native_id=key,
            text=body or summary or key,
            title=summary or key,
            url=url,
            parent_id=key,
            updated_at=updated_at,
            authors=authors,
        )
    ]
    for comment in comments or []:
        comment_id = comment["id"]
        chunks.append(
            Chunk(
                source="jira",
                native_id=f"{key}:comment:{comment_id}",
                text=comment.get("body", ""),
                title=f"{key} comment",
                url=url,
                parent_id=key,
                updated_at=comment.get("updated_at") or updated_at,
                authors=tuple(
                    [comment["author"]] if comment.get("author") else []
                ),
            )
        )
    return chunks


def chat_chunk(
    *,
    message_id: str,
    text: str,
    channel: str,
    thread_id: str = "",
    url: str = "",
    created_at: str | None = None,
    author: str = "",
) -> Chunk:
    return Chunk(
        source="chat",
        native_id=message_id,
        text=text,
        title=channel,
        url=url,
        parent_id=thread_id or message_id,
        thread_id=thread_id or message_id,
        channel_or_space=channel,
        created_at=created_at,
        updated_at=created_at,
        authors=(author,) if author else (),
    )
