from __future__ import annotations

import logging
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from ragtone.ingest.run import open_index
from ragtone.settings import Settings

log = logging.getLogger(__name__)


def find_compose_file(start: Path | None = None) -> Path | None:
    roots: list[Path] = []
    if start is not None:
        roots.append(start)
    roots.append(Path.cwd())
    roots.append(Path(__file__).resolve().parents[2])
    seen: set[Path] = set()
    for root in roots:
        path = (root / "docker-compose.yml").resolve()
        if path in seen:
            continue
        seen.add(path)
        if path.is_file():
            return path
    return None


def child_argv(command: str, *, config: Path | None = None, extra: list[str] | None = None) -> list[str]:
    cmd = [sys.executable, "-m", "ragtone"]
    if config is not None:
        cmd += ["--config", str(config)]
    cmd.append(command)
    if extra:
        cmd.extend(extra)
    return cmd


def ensure_elasticsearch(settings: Settings, *, compose_file: Path | None = None) -> None:
    if open_index(settings).ping():
        log.info("elasticsearch already up at %s", settings.elasticsearch_url)
        return
    compose = compose_file or find_compose_file()
    if compose is None:
        raise SystemExit(
            f"Elasticsearch not reachable at {settings.elasticsearch_url} "
            "and docker-compose.yml was not found"
        )
    log.info("starting Elasticsearch via docker compose (%s)", compose)
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose), "up", "-d"],
        cwd=str(compose.parent),
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit("docker compose up failed — is Docker running?")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if open_index(settings).ping():
            log.info("elasticsearch ready")
            return
        time.sleep(2)
    raise SystemExit("Elasticsearch did not become ready in 120s")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=4)


def run_up(
    settings: Settings,
    *,
    config: Path | None = None,
    backfill: bool = False,
    sync: bool = True,
    open_browser: bool = True,
) -> None:
    ensure_elasticsearch(settings)
    index = open_index(settings)
    index.ensure_index()

    if backfill:
        log.info("running ingest --backfill before the stack stays up")
        ingest = subprocess.run(child_argv("ingest", config=config, extra=["--backfill"]))
        if ingest.returncode != 0:
            log.warning("backfill exited %s — starting the stack anyway", ingest.returncode)

    names = ["serve", "board"]
    if sync:
        names.append("sync")
    procs: dict[str, subprocess.Popen] = {}
    try:
        for name in names:
            procs[name] = subprocess.Popen(child_argv(name, config=config))
        board_url = f"http://{settings.board_host}:{settings.board_port}"
        mcp_url = f"http://{settings.mcp_host}:{settings.mcp_port}/mcp"
        print(f"Trilha  {board_url}")
        print(f"MCP2    {mcp_url}")
        print("Ctrl+C para parar")
        if open_browser:
            try:
                webbrowser.open(board_url)
            except Exception:
                log.warning("could not open the browser")
        _supervise(procs)
    finally:
        for proc in procs.values():
            _stop(proc)


def _supervise(procs: dict[str, subprocess.Popen]) -> None:
    try:
        while True:
            for name, proc in procs.items():
                code = proc.poll()
                if code is None:
                    continue
                log.warning("%s exited with %s", name, code)
                if name in {"serve", "board"}:
                    return
            if all(proc.poll() is not None for proc in procs.values()):
                return
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\nstopping")
