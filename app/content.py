"""内容层：读取 content/*.json 并给出内容版本。

版本号 = 文件内容的 sha1 前 8 位。改一个字版本就变，
客户端据此判断要不要重新下载——不需要手工维护版本号。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_cache: dict[str, tuple[float, str]] = {}


def version(content_dir: Path, name: str) -> str:
    path = content_dir / f"{name}.json"
    if not path.exists():
        return "0"
    mtime = path.stat().st_mtime
    cached = _cache.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    digest = hashlib.sha1(path.read_bytes()).hexdigest()[:8]
    _cache[name] = (mtime, digest)
    return digest


def load(content_dir: Path, name: str) -> Any:
    path = content_dir / f"{name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
