"""System-discoverable option lists.

Each provider returns a sorted list of valid values for some kind of field
(fonts, timezones, etc.). Results are cached in-process so a slow command like
`fc-list` runs once per session. All providers fail gracefully — an empty list
on any error means the heuristic layer treats the key as unsupported and
leaves the widget as a plain text input.
"""
from __future__ import annotations

import platform
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path


def _run(cmd: list[str], timeout: float = 5.0) -> str:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return result.stdout if result.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""


def _which(cmd: str) -> bool:
    return _run(["which", cmd], timeout=1.0).strip() != ""


@lru_cache(maxsize=1)
def list_fonts() -> list[str]:
    """Installed font family names, deduplicated and sorted.

    Prefers fc-list (fontconfig) — fast, cross-platform, parses cleanly.
    Falls back to macOS's system_profiler if fc-list isn't installed (slower,
    ~15s on first call but cached after that).
    """
    families: set[str] = set()
    if _which("fc-list"):
        out = _run(["fc-list", ":", "family"], timeout=10.0)
        for line in out.splitlines():
            for name in line.split(","):
                name = name.strip()
                if name:
                    families.add(name)
    elif sys.platform == "darwin":
        out = _run(["system_profiler", "SPFontsDataType"], timeout=30.0)
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("Family:"):
                families.add(stripped[len("Family:"):].strip())
    return sorted(families)


@lru_cache(maxsize=1)
def list_timezones() -> list[str]:
    """IANA timezone IDs, derived from /usr/share/zoneinfo/."""
    root = Path("/usr/share/zoneinfo")
    if not root.is_dir():
        return []
    skip_top = {"posix", "right", "SystemV", "Etc"}
    results: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in skip_top:
            continue
        # Real tz files: at least one slash (Region/City) and no dots in name
        name = str(rel)
        if "/" not in name or "." in path.name:
            continue
        if path.name in {"localtime", "tzdata.zi", "leapseconds", "iso3166.tab", "zone.tab", "zone1970.tab"}:
            continue
        results.add(name)
    return sorted(results)


@lru_cache(maxsize=1)
def list_locales() -> list[str]:
    """Output of `locale -a`, filtered to typical lang_REGION[.encoding] values."""
    out = _run(["locale", "-a"], timeout=5.0)
    if not out:
        return []
    results: set[str] = set()
    valid = re.compile(r"^[A-Za-z]{2,3}(_[A-Za-z]{2,3})?(\.[A-Za-z0-9-]+)?(@[A-Za-z0-9]+)?$")
    for line in out.splitlines():
        line = line.strip()
        if line and (line in {"C", "POSIX"} or valid.match(line)):
            results.add(line)
    return sorted(results)


@lru_cache(maxsize=1)
def list_shells() -> list[str]:
    """Login shells listed in /etc/shells (one path per line, comments skipped)."""
    p = Path("/etc/shells")
    if not p.is_file():
        return []
    out: list[str] = []
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return sorted(set(out))


PROVIDERS: dict[str, callable] = {
    "fonts": list_fonts,
    "timezones": list_timezones,
    "locales": list_locales,
    "shells": list_shells,
}
