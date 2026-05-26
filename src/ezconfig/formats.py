"""Format adapters with a common Document interface.

A Document wraps a parsed config and exposes a uniform read/write API keyed by
path tuples (e.g. ("server", "host")). Each adapter preserves the source
format on save — comments and key order where the underlying library allows it.
"""
from __future__ import annotations

import configparser
import io
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import tomlkit
from ruamel.yaml import YAML

Path_ = tuple[str, ...]

DOWNLOADS = Path.home() / "Downloads"


_GENERIC_STEMS = {"config", "settings", "bindings", "preferences", "options", "prefs"}


def _detect_app_name(source: Path) -> str:
    """Best-effort 'application' name for the backup folder.

    Uses the filename stem (leading dot stripped for hidden files). When the
    stem is a generic configuration noun (settings, config, bindings, etc.),
    falls back to the parent directory name — so /path/to/myapp/settings.json
    becomes "myapp", not "settings".

    Examples: myapp.conf→myapp, .myapprc→myapprc, ~/.ssh/config→ssh,
    /path/to/foo/settings.json→foo, tsconfig.json→tsconfig.
    """
    stem = source.stem or source.name
    if stem.startswith("."):
        stem = stem[1:]
    if not stem or stem.lower() in _GENERIC_STEMS:
        parent = source.parent.name.lstrip(".")
        if parent:
            return parent
    return stem or "unknown"


def backup_to_downloads(source: Path, format_name: str = "") -> Path | None:
    """On first save, copy `source` (with comments intact) into
    ~/Downloads/<app> config/ and write an info.txt. On subsequent saves, leave
    the original copy untouched and append a save-log line to info.txt.

    Returns the path to the preserved original (or None when source doesn't
    exist yet — nothing to back up).
    """
    if not source.exists():
        return None
    abs_source = source.resolve()
    app = _detect_app_name(abs_source)
    folder = DOWNLOADS / f"{app} config"
    folder.mkdir(parents=True, exist_ok=True)
    original_copy = folder / abs_source.name
    info_path = folder / "info.txt"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    first_save = not original_copy.exists()
    if first_save:
        shutil.copy2(abs_source, original_copy)

    if first_save or not info_path.exists():
        header = (
            "ezconfig backup\n"
            "================\n\n"
            f"Source path: {abs_source}\n"
            f"Format:      {format_name or 'unknown'}\n"
            f"App:         {app}\n"
            f"Backed up:   {now}\n\n"
            "Save log:\n"
            f"  {now} — first save (original preserved as {abs_source.name})\n"
        )
        info_path.write_text(header, encoding="utf-8")
    else:
        with info_path.open("a", encoding="utf-8") as f:
            f.write(f"  {now} — saved\n")

    return original_copy


class Document:
    """Common interface across formats. Subclasses implement the I/O bits."""

    format_name: str = ""

    def __init__(self, data: Any, source_path: Path):
        self.data = data
        self.source_path = source_path

    def walk(self) -> Iterator[tuple[Path_, Any]]:
        """Yield (path, value) for every leaf in the document."""
        yield from _walk(self.data, ())

    def get(self, path: Path_) -> Any:
        node = self.data
        for part in path:
            node = node[part]
        return node

    def set(self, path: Path_, value: Any) -> None:
        if not path:
            raise ValueError("cannot set root")
        node = self.data
        for part in path[:-1]:
            node = node[part]
        node[path[-1]] = value

    def add_path(self, path: Path_, value: Any) -> list[Path_]:
        """Create the nested key at `path`, building any missing parent dicts.

        Returns the list of parent paths that had to be created (so callers
        can surface 'created section X' messages). Subclasses can override for
        format-aware parent construction (e.g. tomlkit needs real Tables).
        """
        if not path:
            raise ValueError("cannot add at root")
        created: list[Path_] = []
        node = self.data
        for i, part in enumerate(path[:-1]):
            if part not in node or not isinstance(node[part], dict):
                node[part] = self._new_container()
                created.append(path[: i + 1])
            node = node[part]
        node[path[-1]] = value
        return created

    def _new_container(self) -> Any:
        """Return an empty container suitable for nesting in this format."""
        return {}

    def dumps(self) -> str:
        raise NotImplementedError

    def save(self, path: Path | None = None) -> Path | None:
        """Save the document. Backs up the prior on-disk content into
        ~/Downloads/<app> config/ first. Returns the path of the preserved
        original copy (or None if there was no prior file)."""
        target = path or self.source_path
        backup = backup_to_downloads(target, format_name=self.format_name)
        target.write_text(self.dumps(), encoding="utf-8")
        return backup


def _walk(node: Any, prefix: Path_) -> Iterator[tuple[Path_, Any]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, prefix + (str(key),))
    elif isinstance(node, list):
        # Lists are leaves for editor purposes — we don't drill into them.
        yield prefix, node
    else:
        yield prefix, node


class TomlDocument(Document):
    format_name = "toml"

    @classmethod
    def load(cls, path: Path) -> "TomlDocument":
        data = tomlkit.parse(path.read_text(encoding="utf-8"))
        return cls(data, path)

    def dumps(self) -> str:
        return tomlkit.dumps(self.data)

    def _new_container(self) -> Any:
        return tomlkit.table()


class YamlDocument(Document):
    format_name = "yaml"

    _yaml = YAML()
    _yaml.preserve_quotes = True
    _yaml.indent(mapping=2, sequence=4, offset=2)

    @classmethod
    def load(cls, path: Path) -> "YamlDocument":
        data = cls._yaml.load(path.read_text(encoding="utf-8")) or {}
        return cls(data, path)

    def dumps(self) -> str:
        buf = io.StringIO()
        self._yaml.dump(self.data, buf)
        return buf.getvalue()

    def _new_container(self) -> Any:
        from ruamel.yaml.comments import CommentedMap
        return CommentedMap()


class JsonDocument(Document):
    """JSON with key-order preservation. Strips // line comments on load (JSONC)."""

    format_name = "json"

    @classmethod
    def load(cls, path: Path) -> "JsonDocument":
        raw = path.read_text(encoding="utf-8")
        stripped = _strip_jsonc_comments(raw)
        data = json.loads(stripped)
        return cls(data, path)

    def dumps(self) -> str:
        return json.dumps(self.data, indent=2) + "\n"


def _strip_jsonc_comments(src: str) -> str:
    """Strip // line comments and /* block comments */ while respecting strings."""
    out: list[str] = []
    i, n = 0, len(src)
    in_str = False
    str_quote = ""
    while i < n:
        ch = src[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if ch == str_quote:
                in_str = False
            i += 1
            continue
        if ch in ('"', "'"):
            in_str = True
            str_quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            nxt = src[i + 1]
            if nxt == "/":
                while i < n and src[i] != "\n":
                    i += 1
                continue
            if nxt == "*":
                i += 2
                while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                    i += 1
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


_INI_BOOL_TRUE = {"true", "yes", "on", "1"}
_INI_BOOL_FALSE = {"false", "no", "off", "0"}


def _coerce_ini_value(raw: str) -> Any:
    """Coerce a raw INI string into bool/int/float when unambiguous."""
    s = raw.strip()
    lower = s.lower()
    if lower in _INI_BOOL_TRUE:
        return True
    if lower in _INI_BOOL_FALSE:
        return False
    # int (no decimal, optional sign)
    if s and (s[0] in "+-" and s[1:].isdigit() or s.isdigit()):
        try:
            return int(s)
        except ValueError:
            pass
    # float
    if any(c in s for c in ".eE"):
        try:
            return float(s)
        except ValueError:
            pass
    return raw


def _format_ini_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class IniDocument(Document):
    """INI via configparser. Flat: paths are always (section, key).

    Values are type-coerced on load (bool/int/float) so the TUI picks the right
    widget. configparser drops comments — acceptable for v1.
    """

    format_name = "ini"

    @classmethod
    def load(cls, path: Path) -> "IniDocument":
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str  # preserve key casing
        parser.read(path, encoding="utf-8")
        data: dict[str, dict[str, Any]] = {}
        for section in parser.sections():
            data[section] = {k: _coerce_ini_value(v) for k, v in parser.items(section)}
        if parser.defaults():
            data["DEFAULT"] = {k: _coerce_ini_value(v) for k, v in parser.defaults().items()}
        return cls(data, path)

    def dumps(self) -> str:
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        for section, values in self.data.items():
            if section == "DEFAULT":
                for k, v in values.items():
                    parser["DEFAULT"][k] = _format_ini_value(v)
                continue
            parser[section] = {k: _format_ini_value(v) for k, v in values.items()}
        buf = io.StringIO()
        parser.write(buf)
        return buf.getvalue()

    def add_path(self, path: Path_, value: Any) -> list[Path_]:
        """INI is flat: paths must be exactly (section, key)."""
        if len(path) != 2:
            raise ValueError(f"INI paths must be (section, key), got {path}")
        section, key = path
        created: list[Path_] = []
        if section not in self.data:
            self.data[section] = {}
            created.append((section,))
        self.data[section][key] = value
        return created


# Token patterns for text-fallback scanning. Each kind is detected by a
# separate regex; matches that overlap with a higher-priority kind are dropped.
# Priority order: color > bool > number > string.
_HEX_TOKEN_RE = re.compile(r"(?<![\w#])(#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3}))(?![\w])")
_BOOL_TOKEN_RE = re.compile(r"\b(true|false|yes|no|on|off)\b", re.IGNORECASE)
_NUMBER_TOKEN_RE = re.compile(r"(?<![\w.])(-?\d+(?:\.\d+)?)(?![\w.])")
# Quoted strings capture the inner content (no quotes) so the editor can
# replace just the contents and the original quote characters stay in place.
_STRING_TOKEN_RE = re.compile(r"\"([^\"\\\n]*(?:\\.[^\"\\\n]*)*)\"|'([^'\\\n]*(?:\\.[^'\\\n]*)*)'")

# Assignment-style lines: `key = value` or `key: value`. The capture group is
# the LHS+separator; the RHS is whatever follows. Used so we can register the
# whole RHS as a single editable value when no typed token (color/bool/number/
# quoted-string) already covers it — covering categorical values like
# `cursor-style = bar` or `shell-integration = detect` whose accepted set we
# don't know.
_ASSIGN_KEY_RE = re.compile(r"^[ \t]*[A-Za-z_][\w.\-]*[ \t]*[=:][ \t]*")
# Trailing line comment, requires whitespace before # or // so hex values
# (`#1E0A3C`) and URL-ish RHS values aren't truncated.
_TRAILING_COMMENT_RE = re.compile(r"\s+(?:#|//)")

_BOOL_PAIRS = {
    "true": ("true", "false"), "false": ("true", "false"),
    "yes": ("yes", "no"),       "no": ("yes", "no"),
    "on": ("on", "off"),        "off": ("on", "off"),
}


def _overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def _render_bool_preserving_case(value: bool, original: str) -> str:
    """Toggle a bool while keeping the original case style + word set."""
    pair = _BOOL_PAIRS.get(original.lower())
    if pair is None:
        return "true" if value else "false"
    word = pair[0] if value else pair[1]
    if original.isupper():
        return word.upper()
    if original[:1].isupper():
        return word.capitalize()
    return word

_TEXT_WARNINGS = {
    "bool": "Detected by word match — may be a literal string, not a toggle.",
    "number": "Detected by digit pattern — may be a port or identifier, not a tunable.",
    "string": "Quoted string — only inner content is editable.",
    "unquoted": "Free-form — app may only accept a specific set; no validation.",
}

# Identifier-shaped words used for labeling lines. Must START with a letter
# so we skip hex values, version-numbers-as-leading-digits, and quoted strings.
_LINE_WORD_RE = re.compile(r"\b[A-Za-z][\w.\-]*\b")

# Command-like keywords that appear at the start of many DSL lines. When the
# first word on a line is one of these, we prefer the second word as the field
# name. This list is intentionally generic — DSL-agnostic verbs that show up
# across many config languages.
_COMMAND_WORDS = frozenset({
    "set", "let", "define", "bind", "map", "unbind", "unmap",
    "include", "source", "import", "require", "use",
    "alias", "export", "declare",
    "color-link", "color", "highlight", "hi", "syntax",
})


class TextDocument(Document):
    """Fallback adapter for non-standard formats.

    Reads the file as raw text, regex-finds hex color tokens, and exposes each
    as an editable leaf. Saving rewrites the file byte-for-byte except for the
    edited token positions — everything else (comments, blank lines, custom
    syntax) is preserved exactly.

    Best-effort: this adapter can edit tokens it recognizes, but doesn't
    understand the file's structure. Labels are guessed from the word
    immediately preceding each token; duplicate labels are disambiguated with
    a numeric suffix.
    """

    format_name = "text"

    def __init__(self, source_path: Path, lines: list[str], tokens: list[dict]):
        self.lines = lines
        self.tokens = tokens  # each: {kind, line, start, end, value, key, original_text}
        # Per-path warnings for the schema cascade to attach to FieldSpecs.
        self.path_warnings: dict[Path_, str] = {}
        data: dict[str, Any] = {}
        for tok in tokens:
            data[tok["key"]] = tok["value"]
            if tok["kind"] in _TEXT_WARNINGS:
                self.path_warnings[(tok["key"],)] = _TEXT_WARNINGS[tok["kind"]]
        super().__init__(data, source_path)

    _COMMENT_PREFIXES = ("#", "//", ";")

    @classmethod
    def load(cls, path: Path) -> "TextDocument":
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        tokens: list[dict] = []
        seen_labels: dict[str, int] = {}
        for line_idx, line in enumerate(lines):
            stripped = line.lstrip()
            # Full-line comments: don't scan. Avoids matching "no" inside
            # "no extension", "on" inside "on save", numbers in dates, etc.
            if any(stripped.startswith(p) for p in cls._COMMENT_PREFIXES):
                continue
            line_lbl = cls._line_label(line)
            raw_matches = cls._scan_line(line)
            for token_idx, (kind, start, end, raw, parsed) in enumerate(raw_matches):
                base = cls._token_label(line_lbl, token_idx, line_idx)
                seen_labels[base] = seen_labels.get(base, 0) + 1
                key = base if seen_labels[base] == 1 else f"{base}_{seen_labels[base]}"
                tokens.append({
                    "kind": kind,
                    "line": line_idx,
                    "start": start,
                    "end": end,
                    "value": parsed,
                    "original_text": raw,
                    "key": key,
                })
        return cls(path, lines, tokens)

    @staticmethod
    def _scan_line(line: str) -> list[tuple[str, int, int, str, Any]]:
        """Find all editable tokens on one line, resolving overlaps by priority.

        Returns a sorted list of (kind, start, end, original_text, parsed_value).
        Higher-priority kinds preempt lower-priority ones on the same span:
        color > bool > number > string. (A hex inside a quoted string surfaces
        as a color picker, not a string editor.)
        """
        spans: list[tuple[str, int, int, str, Any]] = []

        for m in _HEX_TOKEN_RE.finditer(line):
            spans.append(("color", m.start(1), m.end(1), m.group(1), m.group(1)))
        for m in _BOOL_TOKEN_RE.finditer(line):
            raw = m.group(1)
            parsed = raw.lower() in {"true", "yes", "on"}
            spans.append(("bool", m.start(1), m.end(1), raw, parsed))
        for m in _NUMBER_TOKEN_RE.finditer(line):
            raw = m.group(1)
            parsed = float(raw) if "." in raw else int(raw)
            spans.append(("number", m.start(1), m.end(1), raw, parsed))
        for m in _STRING_TOKEN_RE.finditer(line):
            inner_group = 1 if m.group(1) is not None else 2
            raw = m.group(inner_group)
            spans.append(("string", m.start(inner_group), m.end(inner_group), raw, raw))

        # Whole-RHS fallback for assignment-style lines. Lowest priority — if
        # a typed token already covers the value, the overlap rule below drops
        # this one. Catches categorical values like `cursor-style = bar`.
        assign = _ASSIGN_KEY_RE.match(line)
        if assign:
            value_start = assign.end()
            rest = line[value_start:]
            comment = _TRAILING_COMMENT_RE.search(rest)
            if comment:
                rest = rest[: comment.start()]
            rest_stripped = rest.rstrip()
            if rest_stripped:
                value_end = value_start + len(rest_stripped)
                raw = line[value_start:value_end]
                spans.append(("unquoted", value_start, value_end, raw, raw))

        # Drop tokens whose span overlaps with any higher-priority token.
        priority = {"color": 0, "bool": 1, "number": 2, "string": 3, "unquoted": 4}
        spans.sort(key=lambda s: (priority[s[0]], s[1]))
        kept: list[tuple[str, int, int, str, Any]] = []
        for span in spans:
            kind, s, e, _raw, _parsed = span
            if any(_overlap(s, e, ks, ke) for _, ks, ke, _, _ in kept):
                continue
            kept.append(span)
        kept.sort(key=lambda x: x[1])  # final order: left-to-right
        return kept

    @staticmethod
    def _line_label(line: str) -> str | None:
        """Pick the field-name identifier from a line.

        Heuristic: the field name is the second identifier on the line — the
        first one is typically a command keyword (color-link, set, bind, etc.).
        When the first word is a known command we skip it; otherwise we use
        the first word. Comment lines (starting with # or //) are ignored.
        """
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            return None
        # Strip line-level comments
        line_no_comment = re.split(r"\s+#|\s+//", line, maxsplit=1)[0]
        words = _LINE_WORD_RE.findall(line_no_comment)
        if not words:
            return None
        if len(words) >= 2 and words[0].lower() in _COMMAND_WORDS:
            return words[1]
        return words[0]

    @staticmethod
    def _token_label(line_lbl: str | None, hex_idx_on_line: int, line_idx: int) -> str:
        if line_lbl is None:
            return f"line_{line_idx + 1}"
        if hex_idx_on_line == 0:
            return line_lbl
        return f"{line_lbl}_{hex_idx_on_line + 1}"

    def set(self, path: Path_, value: Any) -> None:
        super().set(path, value)
        # Mirror the change into the matching token so dumps() picks it up.
        if len(path) == 1:
            key = path[0]
            for tok in self.tokens:
                if tok["key"] == key:
                    tok["value"] = value
                    return

    def dumps(self) -> str:
        """Reassemble the file with edited tokens spliced back into position."""
        out_lines = list(self.lines)
        by_line: dict[int, list[dict]] = {}
        for tok in self.tokens:
            by_line.setdefault(tok["line"], []).append(tok)
        for line_idx, toks in by_line.items():
            line = out_lines[line_idx]
            for tok in sorted(toks, key=lambda t: -t["start"]):
                line = line[:tok["start"]] + self._render_token(tok) + line[tok["end"]:]
            out_lines[line_idx] = line
        return "".join(out_lines)

    @staticmethod
    def _render_token(tok: dict) -> str:
        kind = tok["kind"]
        value = tok["value"]
        if kind == "bool":
            return _render_bool_preserving_case(bool(value), tok.get("original_text", "true"))
        # color/number/string: stringify the live value.
        return str(value)


_EXT_MAP: dict[str, type[Document]] = {
    ".toml": TomlDocument,
    ".yaml": YamlDocument,
    ".yml": YamlDocument,
    ".json": JsonDocument,
    ".jsonc": JsonDocument,
    ".ini": IniDocument,
    ".cfg": IniDocument,
    ".conf": IniDocument,
}


def load(path: Path) -> Document:
    """Load a config file. Dispatches by extension when known; sniffs content
    otherwise; falls back to text-token scanning when sniffing finds nothing."""
    ext = path.suffix.lower()
    cls = _EXT_MAP.get(ext)
    if cls is not None:
        return cls.load(path)
    sniffed = _sniff_and_load(path)
    if sniffed is not None:
        return sniffed
    return TextDocument.load(path)


def _sniff_and_load(path: Path) -> Document | None:
    """For unknown extensions, peek at the file content and try the most
    likely structured loader. Returns None if no structured format parses."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    stripped = text.lstrip()
    if not stripped:
        return None

    # 1. JSON / JSONC — content starts with { or [ after whitespace.
    if stripped[0] in "{[":
        try:
            doc = JsonDocument.load(path)
            if isinstance(doc.data, (dict, list)):
                return doc
        except (json.JSONDecodeError, ValueError):
            pass

    # 2. TOML — strict. Requires quoted strings and typed values, so passing
    #    here is strong evidence. Reject empty-result parses so we don't
    #    accept files where TOML accepted just comments.
    try:
        toml_doc = TomlDocument.load(path)
        if isinstance(toml_doc.data, dict) and len(toml_doc.data) > 0:
            return toml_doc
    except Exception:
        pass

    # 3. INI — needs at least one [section] header before we believe it.
    if re.search(r"^\s*\[[^\]\n]+\]\s*$", text, re.MULTILINE):
        try:
            ini_doc = IniDocument.load(path)
            if ini_doc.data:
                return ini_doc
        except Exception:
            pass

    # 4. YAML — very permissive (almost any text parses), so we require the
    #    result to be a dict with at least one key.
    try:
        yaml_doc = YamlDocument.load(path)
        if isinstance(yaml_doc.data, dict) and len(yaml_doc.data) > 0:
            return yaml_doc
    except Exception:
        pass

    return None
