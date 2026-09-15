from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_SHORTCODE = re.compile(r":([a-z0-9_+-]{1,40}):", re.IGNORECASE)


@lru_cache(maxsize=1)
def _names() -> dict[str, str]:
    path = Path(__file__).with_name("emoji.json")
    return json.loads(path.read_text(encoding="utf-8"))


def emojize(text: str) -> str:
    if not text or ":" not in text:
        return text
    names = _names()

    def replace(match: re.Match[str]) -> str:
        return names.get(match.group(1).lower(), match.group(0))

    return _SHORTCODE.sub(replace, text)
