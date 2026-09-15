from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, urlparse

from ragtone.models import Hit
from ragtone.settings import Settings

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)
_API = re.compile(
    r"/rest/api/|api\.atlassian\.com|/ex/jira/|/ex/confluence/",
    re.I,
)
_SLACK_TS = re.compile(r"^\d+\.\d+$")
_JIRA_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}-\d+$")


@dataclass(frozen=True)
class Origins:
    atlassian: str = ""
    slack: str = ""

    @classmethod
    def from_settings(cls, settings: Settings) -> Origins:
        return cls(
            atlassian=settings.atlassian_cloud_id,
            slack=settings.slack_workspace,
        )


def site_origin(raw: str) -> str:
    text = (raw or "").strip()
    if not text or _UUID.fullmatch(text):
        return ""
    if "://" not in text:
        text = f"https://{text}"
    parsed = urlparse(text)
    host = (parsed.netloc or parsed.path.split("/", 1)[0]).strip()
    if not host or "." not in host:
        return ""
    scheme = parsed.scheme or "https"
    return f"{scheme}://{host}"


def _stored(url: str, *, site: str = "") -> str:
    text = (url or "").strip()
    if not text or _API.search(text):
        return ""
    if text.startswith(("http://", "https://")):
        return text
    if text.startswith("/") and site:
        return f"{site}{text}"
    return ""


def _jira_key(native_id: str, parent_id: str) -> str:
    key = parent_id or native_id
    if ":comment:" in key:
        key = key.split(":comment:", 1)[0]
    return key.strip()


def _page_id(native_id: str, parent_id: str) -> str:
    ident = (parent_id or native_id).strip()
    head, _, rest = ident.partition(":")
    if rest and head.isdigit():
        return head
    return ident


def _slack_stamp(native_id: str, thread_id: str) -> str:
    for value in (native_id, thread_id):
        if _SLACK_TS.fullmatch(value):
            return f"p{value.replace('.', '')}"
    return ""


def origin_url(
    *,
    source: str,
    native_id: str = "",
    parent_id: str = "",
    thread_id: str = "",
    channel_or_space: str = "",
    url: str = "",
    atlassian: str = "",
    slack: str = "",
) -> str:
    if source == "jira":
        site = site_origin(atlassian)
        key = _jira_key(native_id, parent_id)
        if site and _JIRA_KEY.fullmatch(key):
            return f"{site}/browse/{key}"
        return _stored(url, site=site)
    if source == "confluence":
        site = site_origin(atlassian)
        page_id = _page_id(native_id, parent_id)
        space = channel_or_space.strip()
        if site and page_id.isdigit():
            if space and space.lower() not in {"pages", "overview", "blog"}:
                return f"{site}/wiki/spaces/{quote(space, safe='~')}/pages/{page_id}"
            return f"{site}/wiki/pages/viewpage.action?pageId={page_id}"
        return _stored(url, site=site)
    if source == "chat":
        if native_id.startswith("local:") or thread_id.startswith("local:"):
            return ""
        channel = channel_or_space.strip()
        if not channel:
            return _stored(url)
        site = site_origin(slack)
        stamp = _slack_stamp(native_id, thread_id)
        if site:
            path = f"{site}/archives/{quote(channel, safe='')}"
            return f"{path}/{stamp}" if stamp else path
        stored = _stored(url)
        if stored:
            return stored
        return f"https://slack.com/app_redirect?channel={quote(channel, safe='')}"
    return _stored(url)


def origin_for(hit: Hit | dict, origins: Origins | None = None) -> str:
    data = hit if isinstance(hit, dict) else {
        "source": hit.source,
        "native_id": hit.native_id,
        "parent_id": hit.parent_id,
        "thread_id": hit.thread_id,
        "channel_or_space": hit.channel_or_space,
        "url": hit.url,
    }
    conf = origins or Origins()
    return origin_url(
        source=str(data.get("source") or ""),
        native_id=str(data.get("native_id") or ""),
        parent_id=str(data.get("parent_id") or ""),
        thread_id=str(data.get("thread_id") or ""),
        channel_or_space=str(data.get("channel_or_space") or ""),
        url=str(data.get("url") or ""),
        atlassian=conf.atlassian,
        slack=conf.slack,
    )
