from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ragtone.__main__ import main
from ragtone.settings import Settings
from ragtone.up import child_argv, ensure_elasticsearch, find_compose_file, run_up


class _FakeIndex:
    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.ensured = False

    def ping(self) -> bool:
        return self.ok

    def ensure_index(self) -> None:
        self.ensured = True


def test_child_argv_targets_a_subcommand(tmp_path: Path) -> None:
    config = tmp_path / "ragtone.yaml"
    cmd = child_argv("serve", config=config)
    assert cmd[-1] == "serve"
    assert "up" not in cmd
    assert "--config" in cmd
    assert str(config) in cmd


def test_find_compose_file_in_start_dir(tmp_path: Path) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    assert find_compose_file(tmp_path) == compose.resolve()


def test_ensure_elasticsearch_skips_docker_when_already_up(monkeypatch) -> None:
    ran = []
    monkeypatch.setattr("ragtone.up.open_index", lambda settings: _FakeIndex(True))
    monkeypatch.setattr("ragtone.up.subprocess.run", lambda *a, **k: ran.append(a) or SimpleNamespace(returncode=0))
    ensure_elasticsearch(Settings(embedder="hash"))
    assert ran == []


def test_run_up_starts_serve_board_and_sync(monkeypatch, tmp_path: Path) -> None:
    fake = _FakeIndex(True)
    spawned: list[list[str]] = []

    class _Proc:
        def __init__(self, argv: list[str]) -> None:
            self.argv = argv
            spawned.append(argv)

        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr("ragtone.up.open_index", lambda settings: fake)
    monkeypatch.setattr("ragtone.up.subprocess.Popen", lambda argv: _Proc(argv))
    monkeypatch.setattr("ragtone.up.webbrowser.open", lambda url: opened.append(url))
    opened: list[str] = []
    run_up(Settings(embedder="hash", data_dir=tmp_path), open_browser=True)
    names = [cmd[-1] for cmd in spawned]
    assert names == ["serve", "board", "sync"]
    assert fake.ensured is True
    assert opened == ["http://127.0.0.1:8766"]


def test_main_without_command_runs_up(monkeypatch, tmp_path: Path) -> None:
    called: dict = {}

    def fake_up(settings, **kwargs) -> None:
        called.update(kwargs)
        called["ok"] = True

    monkeypatch.setattr("ragtone.__main__.load_settings", lambda path: Settings(data_dir=tmp_path, embedder="hash"))
    monkeypatch.setattr("ragtone.__main__.run_up", fake_up)
    main([])
    assert called["ok"] is True
    assert called["sync"] is True
    assert called["open_browser"] is True
    assert called["backfill"] is False
