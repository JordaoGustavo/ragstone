from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class FoundationMcp(BaseModel):
    name: str
    transport: Literal["http", "stdio"] = "http"
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class JiraSource(BaseModel):
    enabled: bool = False
    mcp: str = "atlassian"
    search_tool: str = "jira_search"
    get_tool: str = "jira_get_issue"
    jql: str = 'updated >= "{checkpoint}" ORDER BY updated ASC'
    projects: list[str] = Field(default_factory=list)


class ConfluenceSource(BaseModel):
    enabled: bool = False
    mcp: str = "atlassian"
    search_tool: str = "confluence_search"
    get_tool: str = "confluence_get_page"
    cql: str = 'lastModified >= "{checkpoint}"'
    docs: list[str] = Field(default_factory=list)


class ChatSource(BaseModel):
    enabled: bool = False
    mcp: str = "chat"
    history_tool: str = "conversations_history"
    replies_tool: str = "conversations_replies"
    channels: list[str] = Field(default_factory=list)
    channel_windows: dict[str, int] = Field(default_factory=dict)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RAGTONE_",
        env_file=".env",
        extra="ignore",
    )

    elasticsearch_url: str = "http://127.0.0.1:9200"
    elasticsearch_index: str = "ragtone_chunks"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8765
    board_host: str = "127.0.0.1"
    board_port: int = 8766
    poll_seconds: int = 1200
    mcp_pause_seconds: float = 0.5
    backfill_days: int = 365
    embedder: Literal["fastembed", "hash", "http"] = "fastembed"
    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_dims: int = 384
    embed_host: str = "127.0.0.1"
    embed_port: int = 8770
    embed_url: str = ""
    embed_token: str = ""
    data_dir: Path = Path("data")
    foundation_mcps: list[FoundationMcp] = Field(default_factory=list)
    jira: JiraSource = Field(default_factory=JiraSource)
    confluence: ConfluenceSource = Field(default_factory=ConfluenceSource)
    chat: ChatSource = Field(default_factory=ChatSource)

    @property
    def checkpoint_path(self) -> Path:
        return self.data_dir / "checkpoints.json"

    @property
    def board_path(self) -> Path:
        return self.data_dir / "board.json"

    @property
    def watch_path(self) -> Path:
        return self.data_dir / "watches.json"

    def mcp_by_name(self, name: str) -> FoundationMcp:
        for item in self.foundation_mcps:
            if item.name == name:
                return item
        raise KeyError(f"No Foundation MCP named {name!r} in ragtone.yaml")


def load_settings(path: Path | None = None) -> Settings:
    import os

    config_path = path or Path(os.environ.get("RAGTONE_CONFIG", "ragtone.yaml"))
    data: dict = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{config_path} must contain a mapping")
        data = loaded
    settings = Settings.model_validate(data)
    es_url = os.environ.get("ELASTICSEARCH_URL")
    if es_url:
        settings.elasticsearch_url = es_url
    return settings
