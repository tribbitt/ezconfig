"""Format adapters: TOML, YAML, JSON/JSONC, INI. Each keeps comments and
layout on save. Unknown formats fall back to the text format."""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from json.decoder import scanstring
from pathlib import Path
from typing import Any

import tomlkit
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.util import load_yaml_guess_indent

from ezconfig.document import Document, KeyPath, render_bool
from ezconfig.text_format import TextDocument


class TomlDocument(Document):
    format_name = "toml"

    @classmethod
    def parse(cls, text: str, path: Path) -> TomlDocument:
        return cls(tomlkit.parse(text), path, text)

    def dumps(self) -> str:
        return tomlkit.dumps(self.data)

    def plain(self) -> Any:
        return self.data.unwrap()

    def _new_section(self) -> Any:
        return tomlkit.table()


class YamlDocument(Document):
    format_name = "yaml"

    def __init__(self, data: Any, path: Path, text: str, yaml: YAML):
        super().__init__(data, path, text)
        self._yaml = yaml

    @classmethod
    def parse(cls, text: str, path: Path) -> YamlDocument:
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.width = 4096  # never re-wrap long strings
        if text.strip():
            _, indent, seq_offset = load_yaml_guess_indent(text)
            if indent is not None:
                offset = seq_offset or 0
                yaml.indent(mapping=indent, sequence=max(indent, offset + 2), offset=offset)
        data = yaml.load(text) if text.strip() else None
        if data is None:
            data = CommentedMap()
        elif not isinstance(data, (dict, list)):
            raise ValueError("YAML file is a single value, not a mapping")
        return cls(data, path, text, yaml)

    def dumps(self) -> str:
        buf = io.StringIO()
        self._yaml.dump(self.data, buf)
        return buf.getvalue()

    def _new_section(self) -> Any:
        return CommentedMap()


# --- JSON / JSONC ------------------------------------------------------------
# JSON is edited as text so comments, trailing commas and layout survive.


@dataclass
class _Node:
    value: Any
    start: int
    end: int
    # containers only: (key or None, key/element start, node) per entry
    entries: list[tuple[str | None, int, _Node]] = field(default_factory=list)


_NUMBER_RE = re.compile(r"-?(?:0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?")


class _JsoncParser:
    def __init__(self, text: str):
        self.s = text
        self.i = 0

    def parse(self) -> _Node:
        node = self.value()
        self.skip()
        if self.i != len(self.s):
            self.fail("unexpected text after the value")
        return node

    def fail(self, msg: str) -> None:
        line = self.s.count("\n", 0, self.i) + 1
        raise ValueError(f"JSON: {msg} (line {line})")

    def skip(self) -> None:
        s, n = self.s, len(self.s)
        while self.i < n:
            c = s[self.i]
            if c in " \t\r\n\ufeff":
                self.i += 1
            elif s.startswith("//", self.i):
                end = s.find("\n", self.i)
                self.i = n if end < 0 else end
            elif s.startswith("/*", self.i):
                end = s.find("*/", self.i + 2)
                if end < 0:
                    self.fail("unclosed comment")
                self.i = end + 2
            else:
                return

    def value(self) -> _Node:
        self.skip()
        if self.i >= len(self.s):
            self.fail("unexpected end of file")
        c, start = self.s[self.i], self.i
        if c == "{":
            return self.container("}", keyed=True)
        if c == "[":
            return self.container("]", keyed=False)
        if c == '"':
            text, self.i = scanstring(self.s, self.i + 1)
            return _Node(text, start, self.i)
        for word, val in (("true", True), ("false", False), ("null", None)):
            if self.s.startswith(word, self.i):
                self.i += len(word)
                return _Node(val, start, self.i)
        m = _NUMBER_RE.match(self.s, self.i)
        if not m or m.end() == start:
            self.fail(f"unexpected character {c!r}")
        self.i = m.end()
        num = float(m.group()) if m.group(1) or m.group(2) else int(m.group())
        return _Node(num, start, self.i)

    def container(self, close: str, keyed: bool) -> _Node:
        start = self.i
        self.i += 1
        node = _Node({} if keyed else [], start, start)
        while True:
            self.skip()
            if self.s.startswith(close, self.i):
                self.i += 1
                node.end = self.i
                return node
            entry_start = self.i
            key = None
            if keyed:
                if self.s[self.i : self.i + 1] != '"':
                    self.fail("expected a quoted key")
                key, self.i = scanstring(self.s, self.i + 1)
                self.skip()
                if self.s[self.i : self.i + 1] != ":":
                    self.fail("expected ':'")
                self.i += 1
            child = self.value()
            node.entries.append((key, entry_start, child))
            if keyed:
                node.value[key] = child.value
            else:
                node.value.append(child.value)
            self.skip()
            if self.s[self.i : self.i + 1] == ",":
                self.i += 1
            elif not self.s.startswith(close, self.i):
                self.fail(f"expected ',' or '{close}'")


def _line_start(s: str, i: int) -> int:
    return s.rfind("\n", 0, i) + 1


def _line_end(s: str, i: int) -> int:
    end = s.find("\n", i)
    return len(s) if end < 0 else end


def _indent_of(s: str, i: int) -> str:
    start = _line_start(s, i)
    return s[start : start + len(s[start:i]) - len(s[start:i].lstrip(" \t"))]


def _skip_inline(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t":
        i += 1
    return i


class JsonDocument(Document):
    format_name = "json"

    def __init__(self, path: Path, text: str):
        parser = _JsoncParser(text if text.strip() else "{}")
        self._root = parser.parse()
        super().__init__(self._root.value, path, text)
        self.text = text if text.strip() else "{}\n"
        self.unit = self._detect_unit()

    @classmethod
    def parse(cls, text: str, path: Path) -> JsonDocument:
        return cls(path, text)

    def dumps(self) -> str:
        return self.text

    def _detect_unit(self) -> str:
        for _key, start, _node in self._root.entries:
            indent = _indent_of(self.text, start)
            if indent and indent == self.text[_line_start(self.text, start) : start]:
                return indent
        return "  "

    def _replace(self, text: str) -> None:
        parser = _JsoncParser(text)
        self._root = parser.parse()
        self.text = text
        self.data = self._root.value

    def _node(self, path: KeyPath) -> _Node:
        node = self._root
        for part in path:
            for i, (key, _start, child) in enumerate(node.entries):
                if key == part or (key is None and str(i) == part):
                    node = child
                    break
            else:
                raise KeyError(".".join(path))
        return node

    def _render(self, value: Any, at: int, multiline: bool = True) -> str:
        if not multiline or not isinstance(value, (dict, list)) or not value:
            return json.dumps(value, ensure_ascii=False)
        indent = _indent_of(self.text, at)
        rendered = json.dumps(value, indent=self.unit, ensure_ascii=False)
        return rendered.replace("\n", "\n" + indent)

    def set(self, path: KeyPath, value: Any) -> None:
        node = self._node(path)
        old = self.text[node.start : node.end]
        new = self._render(value, node.start, multiline="\n" in old)
        self._replace(self.text[: node.start] + new + self.text[node.end :])

    def add(self, path: KeyPath, value: Any) -> list[KeyPath]:
        depth = 0
        obj = self._root
        while depth < len(path) - 1:
            nxt = next((c for k, _s, c in obj.entries if k == path[depth]), None)
            if nxt is None or not isinstance(nxt.value, dict):
                break
            obj, depth = nxt, depth + 1
        if not isinstance(obj.value, dict):
            raise ValueError("cannot add keys to a list")
        if path[depth] in obj.value and depth == len(path) - 1:
            raise ValueError(f"{'.'.join(path)} already exists")
        if path[depth] in obj.value:
            raise ValueError(f"{'.'.join(path[: depth + 1])} is not an object")
        nested: Any = value
        for part in reversed(path[depth + 1 :]):
            nested = {part: nested}
        self._insert_member(obj, path[depth], nested)
        return [path[: i + 1] for i in range(depth, len(path) - 1)]

    def _insert_member(self, obj: _Node, key: str, value: Any) -> None:
        s = self.text
        inline = "\n" not in s[obj.start : obj.end]
        if not obj.entries:
            base = _indent_of(s, obj.start)
            inner = base + self.unit
            body = json.dumps(value, indent=self.unit, ensure_ascii=False).replace("\n", "\n" + inner)
            text = "{\n" + inner + json.dumps(key) + ": " + body + "\n" + base + "}"
            self._replace(s[: obj.start] + text + s[obj.end :])
            return
        _k, last_start, last = obj.entries[-1]
        after = _skip_inline(s, last.end)
        has_comma = s[after : after + 1] == ","
        if inline:
            member = f", {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}"
            self._replace(s[: last.end] + member + s[last.end :])
            return
        indent = _indent_of(s, last_start)
        body = json.dumps(value, indent=self.unit, ensure_ascii=False).replace("\n", "\n" + indent)
        member = "\n" + indent + json.dumps(key) + ": " + body + ("," if has_comma else "")
        insert_at = _line_end(s, last.end)
        if insert_at > obj.end - 1:  # closing brace on the same line as the last member
            insert_at = after + (1 if has_comma else 0)
        text = s[:insert_at] + member + s[insert_at:]
        if not has_comma:
            text = text[: last.end] + "," + text[last.end :]
        self._replace(text)

    def delete(self, path: KeyPath) -> None:
        parent = self._node(path[:-1])
        idx = next(
            (i for i, (k, _s, _c) in enumerate(parent.entries) if k == path[-1] or (k is None and str(i) == path[-1])),
            None,
        )
        if idx is None:
            raise KeyError(".".join(path))
        s = self.text
        _k, start, node = parent.entries[idx]
        end = node.end
        after = _skip_inline(s, end)
        has_comma = s[after : after + 1] == ","
        if has_comma:
            end = after + 1
        own_line = not s[_line_start(s, start) : start].strip() and (
            not s[end : _line_end(s, end)].strip() or s[end : _line_end(s, end)].lstrip().startswith("//")
        )
        cuts: list[tuple[int, int]] = []
        if own_line:
            cuts.append((_line_start(s, start), min(_line_end(s, end) + 1, len(s))))
            is_last = idx == len(parent.entries) - 1
            if is_last and not has_comma and idx > 0:
                prev = parent.entries[idx - 1][2]
                comma = _skip_inline(s, prev.end)
                if s[comma : comma + 1] == ",":
                    cuts.append((comma, comma + 1))
        elif has_comma:
            cuts.append((start, _skip_inline(s, end)))
        elif idx > 0:
            cuts.append((parent.entries[idx - 1][2].end, end))
        else:
            cuts.append((start, end))
        for a, b in sorted(cuts, reverse=True):
            s = s[:a] + s[b:]
        self._replace(s)


# --- INI ---------------------------------------------------------------------
# INI is edited line by line so comments and layout survive.

_SECTION_RE = re.compile(r"^\s*\[([^\]\n]+)\]\s*(?:[#;].*)?$")
_ENTRY_RE = re.compile(r"^([ \t]*)([^\s=:#;\[][^=:\n]*?)([ \t]*[=:][ \t]*)(.*?)[ \t]*$")


def _ini_value(raw: str) -> Any:
    low = raw.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if re.fullmatch(r"[+-]?\d+", raw):
        return int(raw)
    if re.fullmatch(r"[+-]?\d*\.\d+([eE][+-]?\d+)?", raw):
        return float(raw)
    return raw


@dataclass
class _IniEntry:
    section: str | None
    key: str
    first: int  # first line index
    end: int  # one past the last line (continuation lines included)
    indent: str
    sep: str
    raw: str


class IniDocument(Document):
    format_name = "ini"

    def __init__(self, path: Path, text: str):
        super().__init__({}, path, text)
        self.newline = "\r\n" if "\r\n" in text else "\n"
        self._load_lines(text)

    @classmethod
    def parse(cls, text: str, path: Path) -> IniDocument:
        return cls(path, text)

    def dumps(self) -> str:
        return "".join(self.lines)

    def _load_lines(self, text: str) -> None:
        self.lines = text.splitlines(keepends=True)
        self.sections: dict[str, int] = {}  # name -> header line index
        self.entries: dict[KeyPath, _IniEntry] = {}
        data: dict[str, Any] = {}
        section: str | None = None
        current: _IniEntry | None = None
        for i, line in enumerate(self.lines):
            body = line.rstrip("\r\n")
            stripped = body.strip()
            indent_len = len(body) - len(body.lstrip())
            if current and stripped and indent_len > len(current.indent) and not stripped.startswith(("#", ";")):
                current.end = i + 1
                continue
            current = None
            if not stripped or stripped.startswith(("#", ";")):
                continue
            if m := _SECTION_RE.match(body):
                section = m.group(1).strip()
                self.sections[section] = i
                data.setdefault(section, {})
                continue
            if m := _ENTRY_RE.match(body):
                indent, key, sep, raw = m.groups()
                current = _IniEntry(section, key, i, i + 1, indent, sep, raw)
                path = (section, key) if section is not None else (key,)
                self.entries[path] = current
        for entry in self.entries.values():
            value = self._value(entry)
            if entry.section is None:
                data[entry.key] = value
            else:
                data[entry.section][entry.key] = value
        self.data = data

    def _value(self, entry: _IniEntry) -> Any:
        if entry.end - entry.first == 1:
            return _ini_value(entry.raw)
        rest = [line.strip() for line in self.lines[entry.first + 1 : entry.end] if line.strip()]
        return ([entry.raw] if entry.raw else []) + rest

    def _entry_lines(self, entry: _IniEntry, value: Any) -> list[str]:
        nl = self.newline
        if isinstance(value, list):
            sep = entry.sep.rstrip()
            return [f"{entry.indent}{entry.key}{sep}{nl}"] + [f"{entry.indent}    {v}{nl}" for v in value]
        if isinstance(value, bool):
            text = render_bool(value, entry.raw or "true")
        else:
            text = str(value)
        return [f"{entry.indent}{entry.key}{entry.sep}{text}{nl}"]

    def _rewrite(self, lines: list[str]) -> None:
        self._load_lines("".join(lines))

    def set(self, path: KeyPath, value: Any) -> None:
        entry = self.entries.get(path)
        if entry is None:
            raise KeyError(".".join(path))
        lines = list(self.lines)
        lines[entry.first : entry.end] = self._entry_lines(entry, value)
        self._rewrite(lines)

    def add(self, path: KeyPath, value: Any) -> list[KeyPath]:
        if len(path) != 2:
            raise ValueError("INI keys are section.key")
        section, key = path
        if path in self.entries:
            raise ValueError(f"{section}.{key} already exists")
        sep = next(iter(self.entries.values())).sep if self.entries else " = "
        lines = list(self.lines)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += self.newline
        if section in self.sections:
            in_section = [e for e in self.entries.values() if e.section == section]
            indent = in_section[-1].indent if in_section else ""
            at = in_section[-1].end if in_section else self.sections[section] + 1
            entry = _IniEntry(section, key, at, at, indent, sep, "")
            lines[at:at] = self._entry_lines(entry, value)
            created: list[KeyPath] = []
        else:
            if lines and lines[-1].strip():
                lines.append(self.newline)
            lines.append(f"[{section}]{self.newline}")
            lines += self._entry_lines(_IniEntry(section, key, 0, 0, "", sep, ""), value)
            created = [(section,)]
        self._rewrite(lines)
        return created

    def delete(self, path: KeyPath) -> None:
        lines = list(self.lines)
        if path in self.entries:
            entry = self.entries[path]
            del lines[entry.first : entry.end]
        elif len(path) == 1 and path[0] in self.sections:
            start = self.sections[path[0]]
            later = [i for i in self.sections.values() if i > start]
            del lines[start : min(later) if later else len(lines)]
        else:
            raise KeyError(".".join(path))
        self._rewrite(lines)


def _has_section(text: str) -> bool:
    return any(_SECTION_RE.match(line) for line in text.splitlines())


# --- Loading -----------------------------------------------------------------

_BY_SUFFIX: dict[str, type[Document]] = {
    ".toml": TomlDocument,
    ".yaml": YamlDocument,
    ".yml": YamlDocument,
    ".json": JsonDocument,
    ".jsonc": JsonDocument,
    ".ini": IniDocument,
    ".cfg": IniDocument,
    ".conf": IniDocument,
}
_BY_NAME: dict[str, type[Document]] = {
    ".editorconfig": IniDocument,
    ".gitconfig": IniDocument,
    ".gitmodules": IniDocument,
    "gitconfig": IniDocument,
    ".prettierrc": JsonDocument,
    ".eslintrc": JsonDocument,
    ".babelrc": JsonDocument,
}


def _adapter_for(path: Path) -> type[Document] | None:
    return _BY_NAME.get(path.name) or _BY_SUFFIX.get(path.suffix.lower())


def load(path: Path) -> Document:
    """Load a config file. The adapter is chosen by file name, then by content;
    files no structured adapter accepts use the text format."""
    text = path.read_text(encoding="utf-8")
    cls = _adapter_for(path)
    if cls is IniDocument and not _has_section(text):
        cls = None
    if cls is not None:
        return cls.parse(text, path)
    return _sniff(text, path) or TextDocument.parse(text, path)


def new(path: Path) -> Document:
    """Empty document for a file that doesn't exist yet."""
    cls = _adapter_for(path) or TextDocument
    return cls.new(path)


def _sniff(text: str, path: Path) -> Document | None:
    stripped = text.lstrip()
    if not stripped:
        return None
    if stripped[0] in "{[":
        try:
            return JsonDocument.parse(text, path)
        except ValueError:
            pass
    try:
        doc = TomlDocument.parse(text, path)
        if doc.data:
            return doc
    except Exception:
        pass
    if _has_section(text):
        doc = IniDocument.parse(text, path)
        if doc.entries:
            return doc
    try:
        doc = YamlDocument.parse(text, path)
        if isinstance(doc.data, dict) and doc.data:
            return doc
    except Exception:
        pass
    return None
