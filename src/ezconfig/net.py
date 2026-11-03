"""HTTP GET with a disk cache, shared by schema and docs lookups.

Entries live in ~/.cache/ezconfig for one day. When the network fails, a stale
entry is returned instead of nothing. Each URL is fetched at most once per
process.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from ezconfig import __version__

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ezconfig"
TTL_SEC = 24 * 60 * 60
USER_AGENT = f"ezconfig/{__version__}"

# Set by the CLI (--refresh): ignore cache entries younger than TTL_SEC.
refresh = False


@dataclass(frozen=True)
class Response:
    url: str  # final URL after redirects
    text: str


_memo: dict[str, Response | None] = {}
_lock = threading.Lock()


def get(url: str, timeout: float = 8.0) -> Response | None:
    """Return the body of `url`, or None when it can't be fetched or found in cache."""
    with _lock:
        if url in _memo:
            return _memo[url]
    result = _get_uncached(url, timeout)
    with _lock:
        _memo[url] = result
    return result


def get_json(url: str, timeout: float = 8.0) -> Any | None:
    resp = get(url, timeout)
    if resp is None:
        return None
    try:
        return json.loads(resp.text)
    except json.JSONDecodeError:
        return None


def _cache_file(url: str) -> Path:
    return CACHE_DIR / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")


def _read_cache(path: Path) -> Response | None:
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        return Response(url=entry["url"], text=entry["text"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _get_uncached(url: str, timeout: float) -> Response | None:
    cache = _cache_file(url)
    fresh = cache.exists() and time.time() - cache.stat().st_mtime < TTL_SEC
    if fresh and not refresh:
        cached = _read_cache(cache)
        if cached is not None:
            return cached
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
    except requests.RequestException:
        return _read_cache(cache) if cache.exists() else None
    result = Response(url=resp.url, text=resp.text)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"url": result.url, "text": result.text}), encoding="utf-8")
    except OSError:
        pass
    return result
