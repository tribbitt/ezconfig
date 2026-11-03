"""Text format: for files no structured adapter can parse.

Values are found by pattern (colors, booleans, numbers, quoted strings, and
the right-hand side of `key = value` lines). Saving changes only those spans;
every other byte is kept.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ezconfig.document import Document, KeyPath, render_bool

_HEX_RE = re.compile(r"(?<![\w#])(#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3}))(?!\w)")
_BOOL_RE = re.compile(r"\b(true|false|yes|no|on|off)\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"(?<![\w.#-])(-?\d+(?:\.\d+)?)(?![\w.])")
_STRING_RE = re.compile(r"\"([^\"\\\n]*(?:\\.[^\"\\\n]*)*)\"|'([^'\\\n]*(?:\\.[^'\\\n]*)*)'")
# Inline comment: whitespace, then # or //, then whitespace or end of line.
# "#282c34" is not a comment.
_INLINE_COMMENT_RE = re.compile(r"\s+(?:#|//)(?=\s|$)")
_COMMENT_PREFIXES = ("#", "//", ";")
_WORD_RE = re.compile(r"\b[A-Za-z][\w.\-]*\b")

# Assignment styles: `key = value`, `key: value`, `key value`.
_STYLES = {
    "=": re.compile(r"^[ \t]*([A-Za-z_][\w.\-]*)([ \t]*=[ \t]*)"),
    ":": re.compile(r"^[ \t]*([A-Za-z_][\w.\-]*)([ \t]*:[ \t]+)"),
    " ": re.compile(r"^[ \t]*([A-Za-z_][\w.\-]*)([ \t]+)(?=[^\s=:])"),
}

# First words that start a command rather than name a key ("set x 1",
# "color-link comment #aaa"). On such lines the second word is the name.
_COMMAND_WORDS = frozenset({
    "set", "let", "define", "bind", "map", "unbind", "unmap",
    "include", "source", "import", "require", "use",
    "alias", "export", "declare",
    "color-link", "color", "highlight", "hi", "syntax",
})

_PRIORITY = {"color": 0, "bool": 1, "number": 2, "string": 3, "value": 4}

_WARNINGS = {
    "bool": "Found by word match; may be text rather than a switch.",
    "number": "Found by digit pattern; may be an ID rather than a setting.",
    "string": "Only the text inside the quotes is edited.",
    "value": "Any text is accepted; the app may allow only certain values.",
}


@dataclass
class Token:
    kind: str  # color | bool | number | string | value
    line: int
    start: int
    end: int
    raw: str
    value: Any
    key: str  # unique key shown in the editor
    name: str  # key name used for docs and heuristics


def _code_part(line: str) -> str:
    m = _INLINE_COMMENT_RE.search(line)
    return line[: m.start()] if m else line.rstrip("\r\n")


class TextDocument(Document):
    format_name = "text"

    def __init__(self, path: Path, text: str):
        super().__init__({}, path, text)
        self._load(text)

    @classmethod
    def parse(cls, text: str, path: Path) -> TextDocument:
        return cls(path, text)

    def _load(self, text: str) -> None:
        self.lines = text.splitlines(keepends=True)
        self.newline = "\r\n" if "\r\n" in text else "\n"
        self.style, self.separator = self._detect_style()
        self.can_add = self.style is not None
        self.tokens: list[Token] = []
        used: set[str] = set()
        for idx, line in enumerate(self.lines):
            if line.lstrip().startswith(_COMMENT_PREFIXES):
                continue
            assign = self._assignment(line)
            name = assign[0] if assign else self._label(line) or f"line_{idx + 1}"
            for kind, start, end, raw, value in self._scan(line, assign):
                key, n = name, 1
                while key in used:
                    n += 1
                    key = f"{name}_{n}"
                used.add(key)
                self.tokens.append(Token(kind, idx, start, end, raw, value, key, name))
        self.data = {t.key: t.value for t in self.tokens}
        self._by_key = {t.key: t for t in self.tokens}
        kinds = Counter(t.kind for t in self.tokens)
        summary = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())) or "none"
        self.notes = [f"Text format: {len(self.tokens)} values found by pattern ({summary}). Other text is kept as is."]

    def _detect_style(self) -> tuple[str | None, str]:
        counts: Counter[str] = Counter()
        seps: dict[str, Counter[str]] = {s: Counter() for s in _STYLES}
        code_lines = 0
        for line in self.lines:
            stripped = line.strip()
            if not stripped or stripped.startswith(_COMMENT_PREFIXES):
                continue
            code_lines += 1
            for style, regex in _STYLES.items():
                m = regex.match(line)
                if m and not (style == " " and m.group(1).lower() in _COMMAND_WORDS):
                    counts[style] += 1
                    seps[style][m.group(2)] += 1
                    break
        for style in ("=", ":"):
            if counts[style]:
                return style, seps[style].most_common(1)[0][0]
        if code_lines and counts[" "] >= 0.6 * code_lines:
            return " ", seps[" "].most_common(1)[0][0]
        return None, ""

    def _assignment(self, line: str) -> tuple[str, int] | None:
        """(key, value start) when `line` is a key/value line in the file's style."""
        if self.style is None:
            return None
        m = _STYLES[self.style].match(line)
        if not m or (self.style == " " and m.group(1).lower() in _COMMAND_WORDS):
            return None
        return m.group(1), m.end()

    @staticmethod
    def _label(line: str) -> str | None:
        words = _WORD_RE.findall(_code_part(line))
        if not words:
            return None
        if len(words) >= 2 and words[0].lower() in _COMMAND_WORDS:
            return words[1]
        return words[0]

    @staticmethod
    def _scan(line: str, assign: tuple[str, int] | None) -> list[tuple[str, int, int, str, Any]]:
        """Editable spans on one line, left to right. Where spans overlap, the
        higher-priority kind wins: color > bool > number > string > value."""
        code = _code_part(line)
        if assign:
            # Never treat the key itself as a value ("on-save = ..." is not a bool).
            code = " " * assign[1] + code[assign[1]:]
        spans: list[tuple[str, int, int, str, Any]] = []
        for m in _HEX_RE.finditer(code):
            spans.append(("color", m.start(1), m.end(1), m.group(1), m.group(1)))
        for m in _BOOL_RE.finditer(code):
            raw = m.group(1)
            spans.append(("bool", m.start(1), m.end(1), raw, raw.lower() in ("true", "yes", "on")))
        for m in _NUMBER_RE.finditer(code):
            raw = m.group(1)
            spans.append(("number", m.start(1), m.end(1), raw, float(raw) if "." in raw else int(raw)))
        for m in _STRING_RE.finditer(code):
            g = 1 if m.group(1) is not None else 2
            spans.append(("string", m.start(g), m.end(g), m.group(g), m.group(g)))
        if assign:
            start = assign[1]
            end = len(code.rstrip())
            if end > start:
                spans.append(("value", start, end, line[start:end], line[start:end]))
        spans.sort(key=lambda s: (_PRIORITY[s[0]], s[1]))
        kept: list[tuple[str, int, int, str, Any]] = []
        for span in spans:
            if not any(span[1] < k[2] and k[1] < span[2] for k in kept):
                kept.append(span)
        return sorted(kept, key=lambda s: s[1])

    # --- Document API ----------------------------------------------------

    def name_of(self, path: KeyPath) -> str:
        tok = self._by_key.get(path[0]) if path else None
        return tok.name if tok else super().name_of(path)

    def warning_for(self, path: KeyPath) -> str:
        tok = self._by_key.get(path[0]) if path else None
        return _WARNINGS.get(tok.kind, "") if tok else ""

    def set(self, path: KeyPath, value: Any) -> None:
        tok = self._by_key[path[0]]
        tok.value = value
        self.data[tok.key] = value

    def add(self, path: KeyPath, value: Any) -> list[KeyPath]:
        if not self.can_add:
            raise ValueError("keys can't be added: no `key = value` lines in this file")
        if len(path) != 1:
            raise ValueError("this file has no sections; use a plain key name")
        lines = self.dumps().splitlines(keepends=True)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += self.newline
        lines.append(f"{path[0]}{self.separator}{_render(value, 'true')}{self.newline}")
        self._load("".join(lines))
        return []

    def delete(self, path: KeyPath) -> None:
        tok = self._by_key[path[0]]
        if self._assignment(self.lines[tok.line]) is None:
            raise ValueError("only `key = value` lines can be removed")
        lines = self.dumps().splitlines(keepends=True)
        del lines[tok.line]
        self._load("".join(lines))

    def dumps(self) -> str:
        out = list(self.lines)
        for tok in sorted(self.tokens, key=lambda t: (t.line, -t.start)):
            line = out[tok.line]
            out[tok.line] = line[: tok.start] + _render(tok.value, tok.raw) + line[tok.end :]
        return "".join(out)


def _render(value: Any, raw: str) -> str:
    if isinstance(value, bool):
        return render_bool(value, raw if raw.lower() in ("true", "false", "yes", "no", "on", "off") else "true")
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)
