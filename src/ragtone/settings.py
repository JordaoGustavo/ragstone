from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

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
    headers: dict[str, str] = Field(default_factory=dict)


class JiraSource(BaseModel):
    enabled: bool = False
    mcp: str = "atlassian"
    search_tool: str = "jira_search"
    get_tool: str = "jira_get_issue"
    jql: str = 'updated >= "{checkpoint}" ORDER BY updated ASC'
    projects: list[str] = Field(default_factory=list)
    project_cutoffs: dict[str, str] = Field(default_factory=dict)


class ConfluenceSource(BaseModel):
    enabled: bool = False
    mcp: str = "atlassian"
    search_tool: str = "confluence_search"
    get_tool: str = "confluence_get_page"
    cql: str = 'lastModified >= "{checkpoint}"'
    docs: list[str] = Field(default_factory=list)
    doc_cutoffs: dict[str, str] = Field(default_factory=dict)


class ChatSource(BaseModel):
    enabled: bool = False
    mcp: str = "chat"
    history_tool: str = "conversations_history"
    replies_tool: str = "conversations_replies"
    channels: list[str] = Field(default_factory=list)
    channel_windows: dict[str, int] = Field(default_factory=dict)
    channel_cutoffs: dict[str, str] = Field(default_factory=dict)


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
    lease_poll_seconds: int = 5
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
    atlassian_cloud_id: str = ""
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


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text()) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a mapping")
    return loaded


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _local_override_path(config_path: Path) -> Path | None:
    name = config_path.name
    if name.endswith(".local.yaml") or name.endswith(".local.yml"):
        return None
    suffix = ".yml" if name.endswith(".yml") else ".yaml"
    return config_path.with_name(f"{config_path.stem}.local{suffix}")


def load_settings(path: Path | None = None) -> Settings:
    config_path = path or Path(os.environ.get("RAGTONE_CONFIG", "ragtone.yaml"))
    data = _read_yaml_mapping(config_path)
    override_path = _local_override_path(config_path)
    if override_path is not None:
        data = _deep_merge(data, _read_yaml_mapping(override_path))
    settings = Settings.model_validate(data)
    es_url = os.environ.get("ELASTICSEARCH_URL")
    if es_url:
        settings.elasticsearch_url = es_url
    return settings
