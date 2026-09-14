# Ragtone

Local hybrid search over team chat, Jira, and Confluence. Ingest uses Foundation MCPs (your user). Retrieval is MCP2 on `127.0.0.1` for **Claude Code** and **OpenCode**. Elasticsearch 9 holds the index.

Cursor is not a runtime client on the company PC.

## Layout

| Path | Role |
| --- | --- |
| `src/ragtone/ingest/` | MCP **client** of Foundation servers + sync loop |
| `src/ragtone/index.py` | Elasticsearch 9 mappings and BM25 + kNN (RRF) |
| `src/ragtone/mcp_server.py` | MCP2 HTTP tools: `search`, `thread`, `issue`, `page` |
| `src/ragtone/embeddings.py` | Local vectors (`fastembed`, or `hash` in tests) |
| `docker-compose.yml` | Elasticsearch 9.5.3, bound to localhost |
| `.mcp.json` | Claude Code → `http://127.0.0.1:8765/mcp` |
| `opencode.json` | OpenCode → same URL |
| `src/ragtone/board.py` | Thread canvas: pins, links, saved walks |
| `python -m ragtone board` | Infinite canvas on `http://127.0.0.1:8766` |

## Run

```bash
docker compose up -d
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Edit `ragtone.yaml`: Foundation MCP URL or stdio command, enable `jira` / `confluence` / `chat`, set JQL/CQL/channels.

```bash
python -m ragtone ping
python -m ragtone tools
python -m ragtone ingest --backfill
python -m ragtone serve
python -m ragtone board
```

`board` opens a Maestri-like canvas: drop threads, drag to place, pull the copper port to link them. The numbered chain is the path you walked. It is saved in `data/board.json`, so it is still there when you come back. Search uses Elasticsearch when it is up; if the index is down, Enter still drops a local card so you can keep mapping.

In another terminal, the update loop:

```bash
python -m ragtone sync
```

`sync` polls every `poll_seconds` (default 20 minutes) and resumes from `data/checkpoints.json`.

## Clients

Start `serve` before Claude or OpenCode. Both already have project config in this repo.

- Claude Code: `.mcp.json` (`type: http`)
- OpenCode: `opencode.json` (`type: remote`)

MCP2 binds `127.0.0.1` only.

## Tests

```bash
python -m pytest
```
