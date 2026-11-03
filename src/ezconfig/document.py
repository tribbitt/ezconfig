"""Document: the format-independent view of a config file.

Keys are addressed by a KeyPath, e.g. ("server", "port"). List items are
addressed by their index as a string, e.g. ("servers", "0", "host").
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from collections.abc import Iterator

KeyPath = tuple[str, ...]

BACKUP_ROOT = Path.home() / "Downloads"
_GENERIC_STEMS = {"config", "settings", "bindings", "preferences", "options", "prefs"}


def is_container(value: Any) -> bool:
    """True for values shown as sections: mappings and lists that hold mappings or lists."""
    if isinstance(value, dict):
        return True
    return isinstance(value, list) and any(isinstance(v, (dict, list)) for v in value)


def child(node: Any, part: str) -> Any:
    return node[int(part)] if isinstance(node, list) else node[part]


def items(node: Any) -> Iterator[tuple[str, Any]]:
    """(key, value) pairs of a container; list items are keyed by index."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield str(k), v
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield str(i), v


def parse_literal(raw: str) -> Any:
    """Typed value from user input: true/false, int, float, or string.

    Quotes force a string: '"8080"' -> "8080".
    """
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    if re.fullmatch(r"[+-]?\d+", s):
        return int(s)
    if re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+|\d+(\.\d*)?[eE][+-]?\d+)", s):
        return float(s)
    return raw


_BOOL_WORDS = {
    "true": ("true", "false"), "false": ("true", "false"),
    "yes": ("yes", "no"), "no": ("yes", "no"),
    "on": ("on", "off"), "off": ("on", "off"),
    "1": ("1", "0"), "0": ("1", "0"),
}


def render_bool(value: bool, original: str = "true") -> str:
    """Bool as text in the word pair and letter case of `original`."""
    pair = _BOOL_WORDS.get(original.lower(), ("true", "false"))
    word = pair[0] if value else pair[1]
    if original.isupper():
        return word.upper()
    if original[:1].isupper():
        return word.capitalize()
    return word


def plain(value: Any) -> Any:
    """Convert library containers and scalars to JSON-compatible Python values."""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return str(value)


class Document:
    """Base class. Subclasses implement parse() and dumps(); text-based formats
    also override set/add/delete to edit the source text in place."""

    format_name = ""
    can_add = True  # supports adding keys

    def __init__(self, data: Any, path: Path, text: str = ""):
        self.data = data
        self.path = path
        self.original_text = text
        # Caveats shown to the user, e.g. "comments are removed on save".
        self.notes: list[str] = []

    @classmethod
    def load(cls, path: Path) -> Document:
        return cls.parse(path.read_text(encoding="utf-8"), path)

    @classmethod
    def parse(cls, text: str, path: Path) -> Document:
        raise NotImplementedError

    @classmethod
    def new(cls, path: Path) -> Document:
        return cls.parse("", path)

    # --- reading ---------------------------------------------------------

    def walk(self) -> Iterator[tuple[KeyPath, Any]]:
        """Every leaf (scalar or list of scalars) with its path."""
        yield from _walk(self.data, ())

    def get(self, path: KeyPath) -> Any:
        node = self.data
        for part in path:
            node = child(node, part)
        return node

    def has(self, path: KeyPath) -> bool:
        try:
            self.get(path)
            return True
        except (KeyError, IndexError, ValueError, TypeError):
            return False

    def name_of(self, path: KeyPath) -> str:
        """The key name docs and heuristics should match against."""
        return path[-1] if path else ""

    def warning_for(self, path: KeyPath) -> str:
        """Format-specific caveat for one key ("" for none)."""
        return ""

    def plain(self) -> Any:
        return plain(self.data)

    # --- editing ---------------------------------------------------------

    def set(self, path: KeyPath, value: Any) -> None:
        if not path:
            raise ValueError("cannot set the root")
        parent = self.get(path[:-1])
        if isinstance(parent, list):
            parent[int(path[-1])] = value
        else:
            parent[path[-1]] = value

    def add(self, path: KeyPath, value: Any) -> list[KeyPath]:
        """Create the key at `path`, creating missing parent sections.
        Returns the parent paths that were created."""
        if not path:
            raise ValueError("cannot add at the root")
        created: list[KeyPath] = []
        node = self.data
        for i, part in enumerate(path[:-1]):
            if isinstance(node, list):
                node = child(node, part)
                continue
            if part not in node or not isinstance(node[part], dict):
                node[part] = self._new_section()
                created.append(path[: i + 1])
            node = node[part]
        if isinstance(node, list):
            raise ValueError("cannot add keys to a list")
        node[path[-1]] = value
        return created

    def delete(self, path: KeyPath) -> None:
        if not path:
            raise ValueError("cannot delete the root")
        parent = self.get(path[:-1])
        if isinstance(parent, list):
            del parent[int(path[-1])]
        else:
            del parent[path[-1]]

    def _new_section(self) -> Any:
        return {}

    # --- saving ----------------------------------------------------------

    def dumps(self) -> str:
        raise NotImplementedError

    def save(self) -> Path | None:
        """Write the file. The first save also copies the original into the
        backup folder. Returns the backup path (None for a new file)."""
        copy = backup(self.path, self.format_name)
        text = self.dumps()
        write_atomic(self.path, text)
        self.original_text = text
        return copy


def _walk(node: Any, prefix: KeyPath) -> Iterator[tuple[KeyPath, Any]]:
    if is_container(node):
        for key, value in items(node):
            yield from _walk(value, prefix + (key,))
    else:
        yield prefix, node


def write_atomic(target: Path, text: str) -> None:
    """Replace `target` without leaving a partial file; keeps its permissions.
    Symlinks are followed so dotfile links stay intact."""
    target = target.resolve() if target.is_symlink() else target
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if target.exists():
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def backup_dir(source: Path) -> Path:
    """~/Downloads/<app> config, where <app> is the file stem, or the parent
    folder name when the stem is generic (settings.json -> parent folder)."""
    stem = (source.stem or source.name).lstrip(".")
    if not stem or stem.lower() in _GENERIC_STEMS:
        stem = source.parent.name.lstrip(".") or stem
    return BACKUP_ROOT / f"{stem or 'unknown'} config"


def backup(source: Path, format_name: str = "") -> Path | None:
    """Copy `source` into its backup folder once, and log every save in info.txt."""
    if not source.exists():
        return None
    source = source.resolve()
    folder = backup_dir(source)
    folder.mkdir(parents=True, exist_ok=True)
    copy = folder / source.name
    info = folder / "info.txt"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    first = not copy.exists()
    if first:
        shutil.copy2(source, copy)
    if first or not info.exists():
        info.write_text(
            "ezconfig backup\n\n"
            f"Source: {source}\n"
            f"Format: {format_name or 'unknown'}\n\n"
            "Saves:\n"
            f"  {now}  original copied to {copy.name}\n",
            encoding="utf-8",
        )
    else:
        with info.open("a", encoding="utf-8") as f:
            f.write(f"  {now}  saved\n")
    return copy
