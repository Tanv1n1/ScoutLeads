"""Tiny JSON file cache so repeated runs don't re-pay for searches and verifications."""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

TTL = 7 * 24 * 3600


def _path(key: str) -> Path:
    d = Path(os.environ.get('SCOUT_CACHE_DIR', '.scout_cache'))
    return d / (hashlib.sha1(key.encode()).hexdigest() + '.json')


def get(key: str, ttl: int = TTL) -> Optional[Any]:
    if os.environ.get('SCOUT_CACHE') == '0':
        return None
    try:
        p = _path(key)
        if p.exists() and time.time() - p.stat().st_mtime < ttl:
            return json.loads(p.read_text())
    except (OSError, ValueError):
        pass
    return None


def put(key: str, value: Any) -> None:
    if os.environ.get('SCOUT_CACHE') == '0':
        return
    try:
        p = _path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value))
    except (OSError, TypeError):
        pass
