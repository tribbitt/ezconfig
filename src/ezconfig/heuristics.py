"""Options guessed from a key's name, for keys no schema describes.

Two kinds:
- system lists: installed fonts, timezones, locales, shells, editors, pagers,
  and theme files next to the config file;
- common value sets: light/dark modes, log levels.

Every match is by key name, case-insensitive. All results carry a warning
because the app may not accept every option.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any
from collections.abc import Callable

from ezconfig.net import CACHE_DIR, TTL_SEC


@dataclass
class Guess:
    options: list[Any]
    source: str
    warning: str


def _run(cmd: list[str], timeout: float = 5.0) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


@cache
def fonts() -> list[str]:
    """Font families from fc-list, or system_profiler on macOS (slow, so cached
    on disk for a day)."""
    cache_file = CACHE_DIR / "fonts.json"
    try:
        if time.time() - cache_file.stat().st_mtime < TTL_SEC:
            return json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    families: set[str] = set()
    if shutil.which("fc-list"):
        for line in _run(["fc-list", ":", "family"], timeout=10).splitlines():
            families.update(n.strip() for n in line.split(",") if n.strip())
    elif sys.platform == "darwin":
        for line in _run(["system_profiler", "SPFontsDataType"], timeout=60).splitlines():
            line = line.strip()
            if line.startswith("Family:"):
                families.add(line[len("Family:"):].strip())
    result = sorted(f for f in families if not f.startswith("."))  # "."-prefixed families are macOS-internal
    if result:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(result), encoding="utf-8")
        except OSError:
            pass
    return result


@cache
def timezones() -> list[str]:
    root = Path("/usr/share/zoneinfo")
    if not root.is_dir():
        return []
    skip = {"posix", "right", "SystemV", "Etc"}
    names = set()
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if path.is_file() and len(rel.parts) > 1 and rel.parts[0] not in skip and "." not in path.name:
            names.add(rel.as_posix())
    return sorted(names)


@cache
def locales() -> list[str]:
    valid = re.compile(r"^[A-Za-z]{2,3}(_[A-Za-z]{2,3})?(\.[A-Za-z0-9-]+)?(@[A-Za-z0-9]+)?$")
    out = {line.strip() for line in _run(["locale", "-a"]).splitlines()}
    return sorted(x for x in out if x in ("C", "POSIX") or valid.match(x))


@cache
def shells() -> list[str]:
    p = Path("/etc/shells")
    if not p.is_file():
        return []
    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    return sorted({ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")})


def _installed(names: list[str]) -> Callable[[], list[str]]:
    @cache
    def provider() -> list[str]:
        return [n for n in names if shutil.which(n)]
    return provider


_SYSTEM_LISTS: list[tuple[re.Pattern[str], str, Callable[[], list[str]]]] = [
    (re.compile(r"^(font|font[_-]?family|fontfamily|font[_-]?face)$", re.I), "fonts", fonts),
    (re.compile(r"^(timezone|tz|time[_-]?zone)$", re.I), "timezones", timezones),
    (re.compile(r"^(locale|lang|language)$", re.I), "locales", locales),
    (re.compile(r"^(shell|login[_-]?shell|default[_-]?shell)$", re.I), "shells", shells),
    (re.compile(r"^(editor|visual|git[_-]?editor)$", re.I), "editors",
     _installed(["nvim", "vim", "vi", "nano", "micro", "hx", "helix", "emacs", "kak", "code", "subl", "zed"])),
    (re.compile(r"^(pager|git[_-]?pager)$", re.I), "pagers",
     _installed(["less", "more", "most", "bat", "delta", "moar", "ov"])),
]

_VALUE_SETS: list[tuple[re.Pattern[str], list[str]]] = [
    (re.compile(r"^(theme[_-]?mode|appearance|color[_-]?scheme[_-]?mode|colormode|mode)$", re.I),
     ["light", "dark", "auto", "system"]),
    (re.compile(r"^(log[_-]?level|loglevel|verbosity|level)$", re.I),
     ["trace", "debug", "info", "warn", "warning", "error", "fatal", "critical"]),
]

_THEME_KEY_RE = re.compile(r"^(theme|themes?[_-]?name|colou?r[_-]?scheme|colorscheme|colors|skin|style)$", re.I)
_THEME_DIRS = ("themes", "theme", "colorschemes", "colorscheme", "colors", "skins", "styles")


def theme_options(key: str, config_path: Path) -> list[str]:
    """For theme-like keys: stems of files in theme folders next to the config file."""
    if not _THEME_KEY_RE.match(key):
        return []
    names: set[str] = set()
    for name in _THEME_DIRS:
        folder = config_path.parent / name
        if folder.is_dir():
            names.update(p.stem for p in folder.iterdir() if p.is_file() and not p.name.startswith("."))
    return sorted(names)


def guess(key: str, value: Any, config_path: Path | None = None) -> Guess | None:
    """Options for `key`, or None when no rule matches or the list is empty.
    The current value is kept as the first option when it isn't in the list."""
    found: tuple[list[Any], str, str] | None = None
    if config_path is not None:
        options = theme_options(key, config_path)
        if options:
            found = (options, "theme files", "From theme files next to the config; the app may also have built-in themes.")
    if found is None:
        for pattern, label, provider in _SYSTEM_LISTS:
            if pattern.match(key):
                options = provider()
                if not options:
                    return None
                found = (options, label, f"From this system ({label}); the app may not accept every option.")
                break
    if found is None:
        for pattern, options in _VALUE_SETS:
            if pattern.match(key):
                found = (options, "common values", "Guessed from the key name; the app may use other values.")
                break
    if found is None:
        return None
    options, label, warning = found
    options = list(options)
    if isinstance(value, str) and value and value not in options:
        options.insert(0, value)
    return Guess(options=options, source=f"guess ({label})", warning=warning)
