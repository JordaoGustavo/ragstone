from __future__ import annotations

import json
from pathlib import Path


class CheckpointStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, str] = {}
        if path.exists():
            raw = json.loads(path.read_text())
            if isinstance(raw, dict):
                self._data = {str(k): str(v) for k, v in raw.items()}

    def get(self, name: str) -> str | None:
        return self._data.get(name)

    def set(self, name: str, watermark: str) -> None:
        self._data[name] = watermark
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True) + "\n")

    def all(self) -> dict[str, str]:
        return dict(self._data)
