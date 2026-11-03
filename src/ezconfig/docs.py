"""Find config keys, their candidate values and defaults in documentation.

`load()` reads an http(s) URL, a local file (HTML, Markdown, reStructuredText or
plain text, or a commented sample config) or a man page (`man:NAME`) and turns it
into a flat list of blocks. A key is recognised by its position: heading,
definition term, first table cell, bullet lead, man-page entry line or
sample-config line. Its section runs to the next key-position block, and options
and a default are read from that section. Keys only mentioned in prose fall back
to sentence matching.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from functools import cached_property
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from ezconfig import net


@dataclass
class DocsHit:
    options: list[str]
    excerpt: str
    source: str
    default: str | None = None


@dataclass
class _Block:
    kind: str  # heading, dt, li, p, row, pre, entry (man/text), setting (sample config)
    text: str  # inline code is wrapped in backticks
    level: int = 0  # heading level, list depth or indent
    rank: int | None = None  # blocks with rank <= a key's rank close that key's section
    marked: str = ""  # "code" or "bold" when the text starts inside such markup
    cells: list[str] | None = None
    header: list[str] | None = None  # header cells of the table a row belongs to
    is_header: bool = False
    name: str | None = None  # entry/setting key
    value: str | None = None  # setting value
    group: int = 0  # HTML: id of the innermost list/table container


# ---------------------------------------------------------------------------
# Token shapes and filters

_KEY = r"[A-Za-z][A-Za-z0-9_]*(?:[-.][A-Za-z0-9_]+)*"
_KEY_RE = re.compile(_KEY)
# A defensible option value: short, no whitespace.
_VAL_TOKEN = re.compile(r"^[A-Za-z0-9_./+@:\-]{1,40}$")
_NUMBER = re.compile(r"^[-+]?\d+(?:\.\d+)?%?$")
# Never options, whatever the markup says.
_NOISE = {
    "default", "example", "examples", "e.g", "i.e", "etc", "note", "see", "value", "values",
    "option", "options", "string", "integer", "int", "float", "number", "bool", "boolean",
}
# Boolean-like words. Kept only inside an explicit list that also has a
# non-boolean member (e.g. yes/no/ask); a pure yes/no key is a switch, not a dropdown.
_BOOLISH = {"true", "false", "yes", "no", "on", "off", "none", "null"}
# Lowercase English words that start prose lines; never keys when unformatted.
_STOP = {
    "a", "an", "the", "this", "that", "these", "those", "it", "if", "when", "to", "for", "and",
    "or", "but", "not", "of", "in", "on", "by", "with", "from", "as", "at", "is", "are", "be",
    "can", "may", "must", "should", "will", "you", "your", "we", "use", "using", "set", "see",
    "note", "notes", "example", "examples", "default", "defaults", "warning", "tip", "usage",
    "type", "value", "values", "option", "options", "description", "syntax", "returns", "also",
    "since", "deprecated", "important", "caution", "info", "todo", "uncomment", "available",
    "possible", "valid", "otherwise", "however", "then", "there", "here", "some", "all",
    "any", "each", "every", "only", "other", "such", "which", "what", "how", "where", "why",
}
# Text after a lead token that still makes the token a "term" ("`key`: ...", "key - ...").
_FOLLOW = re.compile(r"\s*(?:$|[:=,]|\((?!\d)|[-–—](?:\s|$))")


def _lead(text: str) -> tuple[str, str, str]:
    """Split `text` into (lead token, markup, rest). Markup is "code" or ""."""
    text = text.lstrip()
    m = re.match(r"`([^`]{1,80})`", text)
    if m:
        return m.group(1).strip(), "code", text[m.end():]
    m = re.match(r"[\"“']([^\"”'\s]{1,60})[\"”']", text)
    if m:
        return m.group(1), "quote", text[m.end():]
    m = re.match(r"[\w./+@-]+", text)
    if m:
        tok = m.group(0).rstrip(".")
        return tok, "", text[len(tok):]
    return "", "", text


_FILE_EXTS = {"conf", "toml", "json", "yaml", "yml", "ini", "cfg", "md", "txt", "py", "sh", "lua", "js"}


def _is_key(tok: str) -> bool:
    if not 2 <= len(tok) <= 64 or _KEY_RE.fullmatch(tok) is None:
        return False
    return "." not in tok or tok.rsplit(".", 1)[1].lower() not in _FILE_EXTS


def _plain_key_ok(tok: str) -> bool:
    """Unformatted lead tokens: lowercase-led or multi-segment, never prose words."""
    if tok.lower() in _STOP:
        return False
    return tok[0].islower() or any(c in tok for c in "-_.")


# ---------------------------------------------------------------------------
# HTML

_SKIP_TAGS = {
    "script", "style", "noscript", "nav", "header", "footer", "svg", "iframe", "form",
    "button", "template", "select",
}
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
    "source", "track", "wbr",
}
_CODE_TAGS = {"code", "tt", "kbd", "samp", "var"}
_BOLD_TAGS = {"strong", "b"}
_FLUSH_TAGS = {
    "p", "div", "section", "article", "main", "blockquote", "ul", "ol", "dl", "dd", "table",
    "figure", "details", "summary", "aside", "hr", "br",
}
# When the document declares a main-content container, everything outside it is
# dropped: site chrome (GitHub repo banners, file trees, sidebars) otherwise reads
# like a list of keys.
_MAIN_TAGS = {"main", "article"}
_STRONG_MAIN = re.compile(r"<(?:main|article)\b|\brole\s*=\s*[\"']?main\b", re.I)
# Fallbacks for pages without <main>/<article>; matched against whole id/class tokens.
_MAIN_IDS = {"main", "main-content", "maincontent"}
_MAIN_CLASSES = {"markdown-body", "Box-body", "container--main", "content", "main-content"}


class _HTMLBlocks(HTMLParser):
    def __init__(self, strong_main: bool) -> None:
        super().__init__(convert_charrefs=True)
        self._strong_main = strong_main
        self.blocks: list[_Block] = []
        self._stack: list[str] = []
        self._main_marks: list[int] = []  # stack heights of open main containers
        self._saw_main = False
        self._skip = 0
        self._code = 0
        self._bold = 0
        self._pre = 0
        self._list_depth = 0
        self._groups: list[int] = []  # open ul/ol/dl/table containers
        self._group_seq = 0
        self._buf: list[str] = []
        self._marked = ""
        self._kind = "p"
        self._level = 0
        self._row: list[str] | None = None
        self._row_is_header = False
        self._cell: list[str] | None = None
        self._thead = 0
        self._table_header: list[str] | None = None

    # -- helpers
    def _accepting(self) -> bool:
        return self._skip == 0 and (not self._saw_main or bool(self._main_marks))

    def _target(self) -> list[str]:
        return self._cell if self._cell is not None else self._buf

    def _flush(self) -> None:
        if self._cell is not None:
            return
        raw = "".join(self._buf)
        self._buf = []
        marked, self._marked = self._marked, ""
        if self._pre:
            return
        text = _clean_inline(raw)
        if not text:
            return
        kind, level = self._kind, self._level
        rank = None
        if kind == "heading":
            rank = level
        elif kind == "dt":
            rank = 7
        elif kind == "li":
            rank = 10 + level
        self.blocks.append(_Block(kind=kind, text=text, level=level, rank=rank, marked=marked,
                                  group=self._groups[-1] if self._groups else 0))
        self._kind, self._level = "p", 0

    def _is_main(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        attr = dict(attrs)
        if tag in _MAIN_TAGS or attr.get("role") == "main":
            return True
        if self._strong_main or tag not in ("div", "section"):
            return False
        if attr.get("id") in _MAIN_IDS:
            return True
        return any(c in _MAIN_CLASSES for c in (attr.get("class") or "").split())

    # -- parser callbacks
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _VOID_TAGS:
            self._stack.append(tag)
            if self._skip == 0 and self._is_main(tag, attrs):
                if not self._saw_main:
                    self._saw_main = True
                    self.blocks = []
                    self._buf = []
                self._main_marks.append(len(self._stack))
            if tag in _SKIP_TAGS:
                self._skip += 1
        if not self._accepting():
            return
        if tag in _CODE_TAGS and not self._pre:
            if self._code == 0:
                self._target().append("`")
            self._code += 1
        elif tag in _BOLD_TAGS:
            self._bold += 1
        elif tag == "pre":
            self._flush()
            self._pre += 1
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush()
            self._kind, self._level = "heading", int(tag[1])
        elif tag == "li":
            self._flush()
            self._kind, self._level = "li", self._list_depth
        elif tag == "dt":
            self._flush()
            self._kind, self._level = "dt", 0
        elif tag in ("ul", "ol", "dl", "table"):
            self._flush()
            self._group_seq += 1
            self._groups.append(self._group_seq)
            if tag == "table":
                self._table_header = None
            elif tag != "dl":
                self._list_depth += 1
        elif tag == "thead":
            self._thead += 1
        elif tag == "tr":
            self._flush()
            self._row, self._row_is_header = [], self._thead > 0
        elif tag in ("td", "th"):
            if self._row is None:
                self._flush()
                self._row, self._row_is_header = [], self._thead > 0
            self._cell = []
            if tag == "th":
                self._row_is_header = True
        elif tag in _FLUSH_TAGS:
            if self._cell is not None:
                self._cell.append(" ")
            else:
                self._flush()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag not in self._stack:
            return
        while self._stack:
            open_tag = self._stack.pop()
            self._close(open_tag)
            if self._main_marks and self._main_marks[-1] > len(self._stack):
                self._main_marks.pop()
            if open_tag == tag:
                break

    def _close(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if not self._accepting() and tag not in _MAIN_TAGS:
            return
        if tag in _CODE_TAGS and self._code:
            self._code -= 1
            if self._code == 0:
                self._target().append("`")
        elif tag in _BOLD_TAGS:
            self._bold = max(0, self._bold - 1)
        elif tag == "pre" and self._pre:
            text = "".join(self._buf)
            self._buf = []
            self._pre -= 1
            for line in text.split("\n"):
                if line.strip():
                    self.blocks.append(_Block(kind="pre", text=line.rstrip()))
        elif tag in ("ul", "ol", "dl", "table"):
            self._flush()
            if self._groups:
                self._groups.pop()
            if tag in ("ul", "ol"):
                self._list_depth = max(0, self._list_depth - 1)
        elif tag == "thead":
            self._thead = max(0, self._thead - 1)
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(_clean_inline("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._emit_row()
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6", "li", "dt", "dd") or tag in _FLUSH_TAGS:
            self._flush()

    def _emit_row(self) -> None:
        row, self._row, self._cell = self._row, None, None
        if not row or not any(row):
            return
        if self._row_is_header:
            self._table_header = row
        self.blocks.append(_Block(
            kind="row", text=" | ".join(row), rank=9, cells=row,
            header=None if self._row_is_header else self._table_header,
            is_header=self._row_is_header, group=self._groups[-1] if self._groups else 0,
        ))

    def handle_data(self, data: str) -> None:
        if not self._accepting():
            return
        if self._pre:
            self._buf.append(data)
            return
        data = re.sub(r"\s+", " ", data)
        target = self._target()
        if target is self._buf and not self._marked and data.strip() and not "".join(self._buf).strip(" `"):
            self._marked = "code" if self._code else "bold" if self._bold else "none"
        target.append(data)

    def close(self) -> None:
        super().close()
        if self._row is not None:
            self._emit_row()
        self._flush()


def _clean_inline(raw: str) -> str:
    text = raw.replace("¶", " ").replace("\u00a0", " ")
    text = re.sub(r"`\s*`", " ", text)
    text = re.sub(r"`\s*([^`]*?)\s*`", r"`\1`", text)
    text = re.sub(r"(?:^|\s)#(?=\s|$)", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_html(text: str) -> list[_Block]:
    p = _HTMLBlocks(strong_main=_STRONG_MAIN.search(text) is not None)
    p.feed(text)
    p.close()
    for b in p.blocks:
        b.marked = "" if b.marked == "none" else b.marked
    return p.blocks


# ---------------------------------------------------------------------------
# Markdown

_MD_FENCE = re.compile(r"^\s*(```+|~~~+)")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_BULLET = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$")
_MD_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
_MD_HR = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_MD_HTML_TAG = re.compile(
    r"</?(?:a|b|i|em|strong|code|br|span|div|p|img|sup|sub|kbd|details|summary|small)\b[^>]*>", re.I
)


def _md_inline(s: str) -> tuple[str, str]:
    """Strip Markdown inline syntax; returns (text, lead markup)."""
    s = s.strip()
    lead = "code" if s.startswith("`") or s.startswith("**`") or s.startswith("__`") else (
        "bold" if s.startswith("**") or s.startswith("__") else "")
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", s)
    s = _MD_HTML_TAG.sub("", s)
    s = re.sub(r"``\s?([^`]+?)\s?``", r"`\1`", s)
    s = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), s)
    s = re.sub(r"(?<![\w*])\*([^*\s][^*]*?)\*(?![\w*])", r"\1", s)
    s = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|<>])", r"\1", s)
    return re.sub(r"\s+", " ", s).strip(), lead


def _md_cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells = re.split(r"(?<!\\)\|", s)
    return [_md_inline(c)[0] for c in cells]


def _parse_markdown(text: str) -> list[_Block]:
    blocks: list[_Block] = []
    cur: _Block | None = None  # open paragraph or list item
    cur_lines: list[str] = []
    fence: str | None = None
    table_header: list[str] | None = None

    def close() -> None:
        nonlocal cur, cur_lines
        if cur is not None:
            cur.text, cur.marked = _md_inline(" ".join(cur_lines))
            if cur.text:
                blocks.append(cur)
        cur, cur_lines = None, []

    lines = text.expandtabs(4).split("\n")
    for idx, line in enumerate(lines):
        if fence is not None:
            if line.strip().startswith(fence):
                fence = None
            elif line.strip():
                blocks.append(_Block(kind="pre", text=line.rstrip()))
            continue
        m = _MD_FENCE.match(line)
        if m:
            close()
            fence = m.group(1)[:3]
            continue
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if not stripped:
            close()
            continue
        if stripped.startswith("|") or (
            idx + 1 < len(lines) and "|" in stripped and _MD_TABLE_SEP.match(lines[idx + 1])
        ):
            if _MD_TABLE_SEP.match(stripped):
                if blocks and blocks[-1].kind == "row":
                    blocks[-1].is_header = True
                    blocks[-1].header = None
                    table_header = blocks[-1].cells
                continue
            close()
            if not (blocks and blocks[-1].kind == "row"):
                table_header = None
            cells = _md_cells(stripped)
            blocks.append(_Block(kind="row", text=" | ".join(cells), rank=9, cells=cells,
                                 header=table_header))
            continue
        if _MD_TABLE_SEP.match(stripped) and blocks and blocks[-1].kind == "row":
            continue
        m = _MD_HEADING.match(stripped) if indent < 4 else None
        if m:
            close()
            level = len(m.group(1))
            blocks.append(_Block(kind="heading", text=_md_inline(m.group(2))[0], level=level,
                                 rank=level, marked=_md_inline(m.group(2))[1]))
            continue
        if cur is not None and cur.kind == "p" and len(cur_lines) == 1 and re.fullmatch(r"=+|-+", stripped):
            level = 1 if stripped[0] == "=" else 2
            cur.kind, cur.level, cur.rank = "heading", level, level
            close()
            continue
        if _MD_HR.match(stripped):
            close()
            continue
        m = _MD_BULLET.match(line)
        if m:
            close()
            cur = _Block(kind="li", text="", level=indent, rank=10 + indent)
            cur_lines = [m.group(2)]
            continue
        if cur is not None:
            cur_lines.append(stripped)
            continue
        cur = _Block(kind="p", text="", level=indent, rank=10 + indent)
        cur_lines = [stripped]
    close()
    return blocks


# ---------------------------------------------------------------------------
# Plain text, reStructuredText and man pages

_CAPS_HEADING = re.compile(r"[A-Z][A-Z0-9 _/&,()'-]{2,}")
_RST_UNDERLINE = re.compile(r"([=\-~^*#+`'\".:])\1{2,}")
_RST_DIRECTIVE = re.compile(
    r"\.\.\s+(?:\w+:)?(?:confval|option|describe|data|attribute|envvar|setting|object)::\s*(\S+)"
)
_TEXT_BULLET = re.compile(r"(?:[-*+•·]|\d+[.)])\s+(\S.*)$")
_ENTRY = re.compile(rf"({_KEY})(?=$|\s|=|\[|<)(.*)$")


def _args_shaped(rest: str) -> bool:
    """Whether the text after an entry name looks like an argument signature."""
    r = rest.strip()
    if not r:
        return True
    if len(r) > 40 or r[-1] in ".:;," or len(r.split()) > 4:
        return False
    return not re.search(r"\b(?:the|a|an|is|are|of|to|and|in|for|with)\b", r, re.I)


def _parse_text(text: str) -> list[_Block]:
    text = re.sub(r"``([^`]+)``", r"`\1`", text)
    lines = [ln.rstrip() for ln in text.expandtabs(8).split("\n")]
    blocks: list[_Block] = []
    para: list[str] = []
    para_kind, para_indent = "p", 0
    underline_levels: list[str] = []

    def flush() -> None:
        nonlocal para
        if para:
            joined = re.sub(r"\s+", " ", " ".join(para)).strip()
            if joined:
                blocks.append(_Block(kind=para_kind, text=joined, level=para_indent,
                                     rank=20 + para_indent))
        para = []

    def next_indent(j: int) -> int:
        for ln in lines[j + 1:]:
            if ln.strip():
                return len(ln) - len(ln.lstrip())
        return -1

    i = 0
    prev_blank = True
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if not s:
            flush()
            prev_blank = True
            i += 1
            continue
        indent = len(raw) - len(raw.lstrip())
        was_blank, prev_blank = prev_blank, False
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if indent == 0 and nxt and _RST_UNDERLINE.fullmatch(nxt) and len(nxt) >= len(s) - 1:
            flush()
            ch = nxt[0]
            if ch not in underline_levels:
                underline_levels.append(ch)
            level = min(underline_levels.index(ch) + 1, 6)
            blocks.append(_Block(kind="heading", text=s, level=level, rank=level))
            i += 2
            prev_blank = True
            continue
        if _RST_UNDERLINE.fullmatch(s):
            flush()
            i += 1
            continue
        if indent == 0 and _CAPS_HEADING.fullmatch(s):
            flush()
            blocks.append(_Block(kind="heading", text=s, level=1, rank=1))
            i += 1
            continue
        # Man-page subsection titles (.SS/.Ss) sit at indent 3, above the entries at 5-7.
        if 0 < indent < 5 and was_blank and re.fullmatch(r"[A-Z][a-z]+(?: [A-Za-z][a-z]*){0,4}", s):
            flush()
            blocks.append(_Block(kind="heading", text=s, level=2, rank=2))
            i += 1
            continue
        m = _RST_DIRECTIVE.match(s)
        if m:
            flush()
            blocks.append(_Block(kind="entry", text=m.group(1), level=indent, rank=20 + indent,
                                 name=m.group(1)))
            i += 1
            continue
        m = _TEXT_BULLET.match(s)
        if m:
            flush()
            para, para_kind, para_indent = [m.group(1)], "li", indent
            i += 1
            continue
        m = _ENTRY.match(s) if (was_blank or (blocks and blocks[-1].kind in ("heading", "entry")
                                                and not para)) else None
        if m and indent <= 24:
            name, rest = m.group(1), m.group(2)
            inline = re.match(r"(\s{2,})(\S.*)$", rest)
            stop = name.lower() in _STOP
            if inline and not stop:
                flush()
                blocks.append(_Block(kind="entry", text=name, level=indent, rank=20 + indent,
                                     name=name))
                para, para_kind = [inline.group(2)], "p"
                para_indent = indent + len(name) + len(inline.group(1))
                i += 1
                continue
            if not inline and not stop and _args_shaped(rest) and next_indent(i) > indent:
                flush()
                blocks.append(_Block(kind="entry", text=s, level=indent, rank=20 + indent,
                                     name=name))
                i += 1
                continue
        if (para and indent == para_indent) or (para and para_kind == "li" and indent > para_indent):
            para.append(s)
        else:
            flush()
            para, para_kind, para_indent = [s], "p", indent
        i += 1
    flush()
    return blocks


# ---------------------------------------------------------------------------
# Commented sample configs

_SETTING = re.compile(rf"^({_KEY})\s*(=|:)\s*(.*)$")
_SETTING_SPACE = re.compile(rf"^({_KEY})\s+(\S.*)$")
_SECTION = re.compile(r"^\[+\s*([^\]]+?)\s*\]+$")


def _setting(body: str, commented: bool) -> tuple[str, str] | None:
    m = _SETTING.match(body)
    if m:
        key, sep, value = m.groups()
        if key.lower() in _STOP or (commented and sep == ":" and value and " " in value.strip()
                                    and not value.strip().startswith(("\"", "'", "[", "{"))):
            return None
        return key, _strip_value(value)
    m = _SETTING_SPACE.match(body)
    if not m:
        return None
    key, value = m.groups()
    if key.lower() in _STOP:
        return None
    if commented:
        value_s = value.strip()
        if (len(key) < 3 or len(value_s.split()) > 3 or value_s[-1] in ".:;,"
                or re.search(r"\b(?:the|a|an|is|are|to|of|and|or|for|with|this|it)\b", value_s, re.I)):
            return None
    return key, _strip_value(value)


def _strip_value(value: str) -> str:
    v = value.strip()
    if v[:1] in ("\"", "'"):
        q = v[0]
        end = v.find(q, 1)
        return v[1:end] if end > 0 else v[1:]
    v = re.sub(r"\s+[#;].*$", "", v)
    return v.strip()


def _parse_sample(text: str) -> list[_Block]:
    """Settings become key blocks; the comment block directly above a setting (or,
    failing that, directly below it) is emitted after it as its description."""
    blocks: list[_Block] = []
    pending: list[str] = []  # comment lines since the last blank line or setting
    below: _Block | None = None  # setting without a description above, open to one below

    def emit_comments(lines: list[str]) -> None:
        for b in _parse_markdown("\n".join(lines)) if lines else ():
            if b.kind != "heading":
                b.rank = None if b.kind == "p" else b.rank
                blocks.append(b)

    def add_setting(hit: tuple[str, str], line: str) -> None:
        nonlocal pending, below
        key, value = hit
        block = _Block(kind="setting", text=line, rank=8, name=key, value=value or None)
        blocks.append(block)
        emit_comments(pending)
        below = None if pending else block
        pending = []

    for raw in [*text.split("\n"), ""]:
        s = raw.strip()
        if not s:
            if below is not None:
                emit_comments(pending)
            pending, below = [], None
            continue
        if s.startswith(("#", ";", "//")):
            body = re.sub(r"^(?:#+:?|;+|//)", "", s)
            hit = _setting(body.strip(), commented=True)
            if hit:
                add_setting(hit, body.strip())
            else:
                pending.append(body.rstrip())
            continue
        m = _SECTION.match(s)
        if m:
            blocks.append(_Block(kind="heading", text=m.group(1), level=1, rank=1))
            pending, below = [], None
            continue
        hit = _setting(s, commented=False)
        if hit:
            add_setting(hit, s)
    return blocks


# ---------------------------------------------------------------------------
# Option and default extraction

_TRIGGER = re.compile(
    r"(?P<strong>"
    r"\b(?:valid|possible|allowed|supported|accepted|acceptable|available|permitted|legal|recognized)"
    r"\s+(?:values?|options?|settings?|choices|arguments?|modes?|styles?)\b"
    r"(?:\s+(?:for|of)\s+(?:this|the|that)\s+[\w-]+)?(?:\s+(?:are|is|include|includes))?"
    r"|\bone\s+of(?:\s+the\s+following(?:\s+values)?)?"
    r"|\b(?:the\s+)?(?:values|choices)\s+(?:are|include)\b|\bthe\s+values\b|\bvalues\s*:"
    r"|\b(?:the\s+)?argument\s+(?:must|may|can|should)\s+be\b"
    r")"
    r"|(?P<weak>\beither\b|\b(?:can|may|must|should|could)\s+(?:also\s+)?be(?!\s+set\s+to)\b)"
    r"|(?P<setto>\bset\s+(?:(?:it|this|this\s+(?:option|value|setting|flag))\s+)?to\b)",
    re.I,
)
_LIST_SPLIT = re.compile(r"\s*,\s*(?:(?:and|or)\s+)?|\s+(?:or|and)\s+|\s*\|\s*")
_MARKED_ITEM = re.compile(
    r"`([^`]{1,60})`"
    r"|(?<!\w)[\"“](?=\S)([^\"”\n]{1,40}?)(?<=\S)[\"”](?!\w)"
    r"|(?<!\w)'([^'\s]{1,40})'(?!\w)"
)
_PLAIN_ITEM = re.compile(r"[\w./+@:-]{1,40}")
_ALT_TOK = r"(?:`[^`|]{1,40}`|\"[^\"|]{1,40}\"|'[^'|\s]{1,40}'|[\w./+@:-]{1,40})"
_ALTERNATION = re.compile(rf"(?<![\w|]){_ALT_TOK}(?:\s*\|\s*{_ALT_TOK})+")
_BRACES = re.compile(r"\{\s*([\w./+@:-]+(?:\s*,\s*[\w./+@:-]+)+)\s*\}")
_MARKED_LIST = re.compile(
    r"(?:`[^`]{1,40}`|\"[^\"]{1,40}\")(?:\s*,\s*(?:`[^`]{1,40}`|\"[^\"]{1,40}\")){1,}"
    r"\s*,?\s*(?:or|and)\s+(?:`[^`]{1,40}`|\"[^\"]{1,40}\")"
)
# "Top, bottom, and hidden are supported." — a list that precedes its trigger.
_LIST_BEFORE = re.compile(
    r"((?:[`\"]?[\w./+@:-]{1,40}[`\"]?\s*,\s*)+(?:and|or)\s+[`\"]?[\w./+@:-]{1,40}[`\"]?)"
    r"\s+(?:are|is)\s+(?:all\s+)?(?:supported|valid|allowed|accepted|available|recognized)\b",
    re.I,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(`'\"\[])|;\s+")
_DEFAULT = re.compile(
    r"\bdefault(?:\s+value|\s+setting)?(?:\s+(?:is|was)\s+|\s*[:=]\s*)(?:set\s+to\s+)?"
    r"|\bdefaults\s+to\s+"
    r"|\(\s*default(?:s\s+to|\s+is|\s*:|\s*=)?\s+",
    re.I,
)
_DEFAULT_AFTER = re.compile(
    r"(`[^`]{1,60}`|\"[^\"]{1,60}\"|[\w./+@:-]{1,40})\s*\((?:the\s+)?default\)", re.I
)
_DEFAULT_STOP = {"the", "a", "an", "to", "this", "that", "it", "is", "be", "value", "same", "not",
                 "depends", "determined", "used", "set", "for", "when", "if", "only"}
# Words that end a prose list ("can be used to ..."); plain list items never match these.
_FUNCTION_WORDS = {
    "a", "an", "the", "this", "that", "these", "those", "it", "its", "to", "for", "of", "in", "by",
    "with", "from", "as", "at", "is", "are", "be", "been", "used", "any", "each", "every", "some",
    "other", "such", "which", "what", "also", "not", "only", "following", "more", "than", "set",
    "specified", "given", "you", "your", "we", "can", "may", "must", "will", "should", "or", "and",
}


def _unquote(tok: str) -> str:
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] + tok[-1] in ("``", "\"\"", "''", "“”"):
        tok = tok[1:-1].strip()
    return tok


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def _list_items(s: str) -> list[str]:
    """Items of a value list that starts at `s` ("`a`, `b`, or `c`. More prose")."""
    s = re.sub(r"^either\s+", "", s.lstrip(" :"), flags=re.I)
    items: list[str] = []
    for part in _LIST_SPLIT.split(s):
        part = re.sub(r"\s*\([^)]*\)", "", part).strip().rstrip(".:")
        if not part:
            continue
        m = _MARKED_ITEM.fullmatch(part)
        if m:
            items.append(next(g for g in m.groups() if g is not None).strip())
            continue
        if _PLAIN_ITEM.fullmatch(part) and part.lower() not in _FUNCTION_WORDS:
            items.append(part)
            continue
        m = _MARKED_ITEM.match(part)
        if m:
            items.append(next(g for g in m.groups() if g is not None).strip())
        break
    return items


def _first_item(s: str) -> tuple[str, bool] | None:
    """The single value right after "set to": (value, was it code/quoted)."""
    s = s.lstrip(" :")
    m = _MARKED_ITEM.match(s)
    if m:
        return next(g for g in m.groups() if g is not None).strip(), True
    m = _PLAIN_ITEM.match(s)
    if m and m.group(0).rstrip(".,").lower() not in _FUNCTION_WORDS:
        return m.group(0).rstrip(".,"), False
    return None


def _explicit_values(text: str) -> tuple[list[str], list[tuple[str, bool]]]:
    """(explicit list values, single values after "set to") found in prose `text`."""
    found: list[str] = []
    singles: list[tuple[str, bool]] = []
    for m in _ALTERNATION.finditer(text):
        found += [_unquote(t) for t in re.split(r"\s*\|\s*", m.group(0))]
    for m in _BRACES.finditer(text):
        found += [t.strip() for t in m.group(1).split(",")]
    for sentence in _sentences(text):
        for m in _TRIGGER.finditer(sentence):
            rest = sentence[m.end():]
            if m.group("setto"):
                single = _first_item(rest)
                if single:
                    singles.append(single)
                continue
            items = _list_items(rest)
            if (m.group("strong") and items) or len(items) >= 2:
                found += items
        for m in _MARKED_LIST.finditer(sentence):
            found += [next(g for g in mm.groups() if g is not None)
                      for mm in _MARKED_ITEM.finditer(m.group(0))]
        for m in _LIST_BEFORE.finditer(sentence):
            items = [_unquote(t) for t in _LIST_SPLIT.split(m.group(1)) if t.strip()]
            # Sentence-initial capital: "Top, bottom, and hidden" means top/bottom/hidden.
            if items[0][:1].isupper() and items[0][1:].islower() and all(t.islower() for t in items[1:]):
                items[0] = items[0].lower()
            if not any(t.lower() in _FUNCTION_WORDS for t in items):
                found += items
    return found, singles


def _code_values(text: str) -> list[str]:
    return [next(g for g in m.groups() if g is not None) for m in _MARKED_ITEM.finditer(text)]


def _cell_values(cell: str) -> list[str]:
    alt = _ALTERNATION.search(cell)
    if alt:
        return [_unquote(t) for t in re.split(r"\s*\|\s*", alt.group(0))]
    codes = _code_values(cell)
    if codes:
        return codes
    parts = [p.strip() for p in re.split(r"[,/]|\s+or\s+", cell) if p.strip()]
    if len(parts) >= 2 and all(_PLAIN_ITEM.fullmatch(p) for p in parts):
        return parts
    return []


def _default_in(text: str) -> str | None:
    for m in _DEFAULT.finditer(text):
        value = _default_value(text[m.end():])
        if value is not None:
            return value
    m = _DEFAULT_AFTER.search(text)
    if m:
        return _unquote(m.group(1))
    return None


def _default_value(s: str) -> str | None:
    s = s.lstrip()
    if not s:
        return None
    if s[0] == "`":
        m = re.match(r"`([^`]*)`", s)
        return m.group(1).strip() if m else None
    if s[0] in "\"“'":
        m = re.match(r"[\"“']([^\"”']*)[\"”']", s)
        return m.group(1).rstrip(".,") if m else None
    if s[0] in "{[":
        close = "}" if s[0] == "{" else "]"
        end = s.find(close)
        return s[: end + 1] if 0 < end <= 80 else None
    m = re.match(r"[^\s,;()]+", s)
    if not m:
        return None
    value = m.group(0).rstrip(".:")
    if not value or value.lower() in _DEFAULT_STOP:
        return None
    return value


def _item_token(b: _Block) -> str | None:
    """Lead token of a list item / table row / term that names one value."""
    if b.kind == "row":
        if b.is_header or not b.cells:
            return None
        text = b.cells[0]
    elif b.kind in ("li", "entry", "dt"):
        text = b.text
    elif b.kind == "p" and b.marked:
        text = b.text
    else:
        return None
    tok, markup, rest = _lead(text)
    if not tok:
        return None
    if not rest.strip() or (b.kind in ("entry", "row") and (markup or _FOLLOW.match(rest))):
        return tok
    if markup and _FOLLOW.match(rest):
        return tok
    if b.kind in ("li", "p", "dt") and re.match(r"\s*(?:[-–—]\s|:\s)", rest):
        return tok
    return None


def _filter(values: list[str], exclude: set[str], explicit: bool) -> list[str]:
    """Value-shaped, deduped (case-insensitively) options; booleans only beside other values."""
    out: dict[str, str] = {}
    for v in values:
        v = _unquote(v).strip().rstrip(".,;:")
        lv = v.lower()
        if (not v or not _VAL_TOKEN.match(v) or _NUMBER.match(v) or lv in _NOISE or lv in exclude
                or v.startswith(("/", "--")) or "//" in v or (not explicit and len(v) < 2)
                or not (v[-1].isalnum() or v[-1] in "_%")  # fragments like "no-"
                or re.fullmatch(r"-[A-Za-z]", v)):  # command-line flags
            continue
        out.setdefault(lv, v)
    result = list(out.values())
    if not explicit or all(v.lower() in _BOOLISH for v in result):
        result = [v for v in result if v.lower() not in _BOOLISH]
    return result


# ---------------------------------------------------------------------------
# Page and index

_CONTINUATION = re.compile(
    r"^\s*(?:valid\s+values|allowed\s+values|possible\s+values|supported\s+values|accepted\s+values|"
    r"available\s+values|options\b|one\s+of\b|must\s+be\b|can\s+be\b|may\s+be\b|it\s+accepts\b|"
    r"accepts\b|the\s+values\b|set\s+to\b)",
    re.I,
)
_EXCERPT_MAX = 600


@dataclass
class _Key:
    name: str
    rank: int  # lower = stronger; see _Block.rank
    weak: bool  # becomes a value when nested inside a stronger key's section
    rest: str  # text after the name on the key line


_VALUE_INTRO = re.compile(
    r"\b(?:one\s+of|values?|options?|following|can\s+be|may\s+be|choices|supported|available|"
    r"either|modes?|styles?|flags)\b[^:]*:$",
    re.I,
)


def _value_list_groups(blocks: list[_Block]) -> set[int]:
    """HTML list/table containers introduced as a value list ("can be one of:")."""
    groups: set[int] = set()
    seen: set[int] = set()
    for i, b in enumerate(blocks):
        if not b.group or b.group in seen:
            continue
        seen.add(b.group)
        prev = blocks[i - 1] if i else None
        if prev and prev.kind == "p" and prev.group != b.group and _VALUE_INTRO.search(prev.text):
            groups.add(b.group)
    return groups


class _Index:
    def __init__(self, blocks: list[_Block]) -> None:
        self.blocks = blocks
        cands = [_candidate(b) for b in blocks]
        value_groups = _value_list_groups(blocks)
        valid: list[int] = []
        stack: list[tuple[int, int]] = []  # (rank, block index) of open key sections
        listed: set[int] = set()  # terms of value lists; they never close a section
        for i, b in enumerate(blocks):
            c = cands[i]
            if c is not None and stack and b.group in value_groups:
                cands[i] = None  # a term in a list introduced as values ("can be one of:")
                listed.add(i)
                continue
            rank = c.rank if c else b.rank
            if rank is None:
                continue
            while stack and stack[-1][0] >= rank:
                stack.pop()
            if c is None:
                continue
            if c.weak and stack:
                cands[i] = None  # a value or sub-item inside another key's section
                continue
            stack.append((rank, i))
            valid.append(i)
        self.sections: dict[str, list[tuple[int, int]]] = {}
        self.aliases: dict[str, list[tuple[int, int]]] = {}
        self.names: dict[str, str] = {}
        for i in valid:
            c = cands[i]
            assert c is not None
            end = len(blocks)
            for j in range(i + 1, len(blocks)):
                cj = cands[j]
                if cj is not None and cj.name.lower() != c.name.lower():
                    end = j
                    break
                r = blocks[j].rank
                if cj is None and r is not None and r <= c.rank and j not in listed:
                    end = j
                    break
            low = c.name.lower()
            self.sections.setdefault(low, []).append((i, end))
            self.names.setdefault(low, c.name)
            if "." in low:
                self.aliases.setdefault(low.rsplit(".", 1)[1], []).append((i, end))
        self.known = set(self.names)

    @cached_property
    def sentences(self) -> list[tuple[str, str]]:
        out = []
        for b in self.blocks:
            if b.kind == "pre":
                continue
            for s in _sentences(b.text):
                out.append((s, s.lower()))
        return out


def _candidate(b: _Block) -> _Key | None:
    if b.kind == "setting" and b.name:
        return _Key(b.name, 8, False, "")
    if b.kind == "entry" and b.name:
        return _Key(b.name, b.rank if b.rank is not None else 20, True, b.text[len(b.name):])
    if b.kind == "pre":
        return None
    if b.kind == "row":
        if b.is_header or not b.cells:
            return None
        tok, markup, rest = _lead(b.cells[0])
        if not _is_key(tok) or not _FOLLOW.match(rest) or (not markup and not _plain_key_ok(tok)):
            return None
        return _Key(tok, 9, True, "")
    tok, markup, rest = _lead(b.text)
    if not _is_key(tok) or markup == "quote":
        return None
    code = markup == "code" or b.marked == "code"
    if b.kind == "heading":
        if not _FOLLOW.match(rest) or (not code and not _plain_key_ok(tok)):
            return None
        return _Key(tok, b.level, False, rest)
    if b.kind == "dt":
        if not _FOLLOW.match(rest) or (not code and tok.lower() in _STOP):
            return None
        return _Key(tok, 7, False, rest)
    if b.kind == "li":
        if not _FOLLOW.match(rest) or (not code and not _plain_key_ok(tok)):
            return None
        return _Key(tok, b.rank if b.rank is not None else 10, True, rest)
    if b.kind == "p":
        if not b.marked or not _FOLLOW.match(rest):
            return None
        if not code and not _plain_key_ok(tok):
            return None
        assign = re.match(r"\s*=", rest) is not None
        rank = 8 if assign else (b.rank - 1 if b.rank is not None else 9)
        return _Key(tok, rank, not assign, rest)
    return None


class DocsPage:
    """Parsed documentation; queried per key with lookup() or in bulk with discover_keys()."""

    def __init__(self, source: str, blocks: list[_Block]) -> None:
        self.source = source
        self._blocks = blocks
        self._hits: dict[str, DocsHit | None] = {}

    @cached_property
    def _index(self) -> _Index:
        return _Index(self._blocks)

    def lookup(self, key: str) -> DocsHit | None:
        low = key.strip().lower()
        if not low:
            return None
        if low in self._hits:
            return self._hits[low]
        idx = self._index
        spans = idx.sections.get(low) or idx.aliases.get(low)
        if spans:
            hits = [self._section_hit(i, end) for i, end in spans]
            hit = max(enumerate(hits), key=lambda p: (len(p[1].options) > 0, p[1].default is not None,
                                                      len(p[1].options), -p[0]))[1]
        else:
            hit = self._prose_hit(low)
        self._hits[low] = hit
        return hit

    def discover_keys(self) -> dict[str, DocsHit]:
        found: dict[str, DocsHit] = {}
        for low, name in self._index.names.items():
            hit = self.lookup(low)
            if hit is not None:
                found[name] = hit
        return found

    # -- section-based hits
    def _section_hit(self, i: int, end: int) -> DocsHit:
        idx = self._index
        blocks = idx.blocks
        head = blocks[i]
        cand = _candidate(head)
        assert cand is not None
        own = cand.name.lower()
        # Values never include the key itself; loose code spans also skip other keys' names.
        own_names = {own, own.rsplit(".", 1)[-1]}
        other_keys = idx.known | own_names
        explicit: list[str] = []
        singles: list[tuple[str, bool]] = []
        loose: list[str] = []
        default: str | None = None
        texts: list[str] = []

        if head.kind == "row" and head.cells:
            for col, cell in enumerate(head.cells[1:], start=1):
                label = (head.header[col] if head.header and col < len(head.header) else "").lower()
                if "default" in label:
                    d = _code_values(cell) or [cell.strip()]
                    default = default or (d[0] if d[0] and len(d[0]) <= 80 else None)
                elif re.search(r"value|option|choice|allowed|accepted|possible|type", label):
                    explicit += _cell_values(cell)
                else:
                    texts.append(cell)
        elif head.kind == "setting":
            pass
        else:
            texts.append(cand.rest)

        pre_default: str | None = head.value if head.kind == "setting" else None
        # A `key value` code line right under the key states the default (kitty, many
        # Sphinx sites); it beats prose, where "default" often refers to something else.
        leading_default: str | None = None
        for j in range(i + 1, end):
            b = blocks[j]
            if b.kind == "pre":
                if pre_default is None:
                    m = re.match(rf"\s*{re.escape(cand.name)}\s*(?:[=:]\s*|\s+)(\S.*)$", b.text, re.I)
                    if m:
                        pre_default = _strip_value(m.group(1)) or None
                        if j == i + 1:
                            leading_default = pre_default
                continue
            tok = _item_token(b)
            if tok is not None:
                explicit.append(tok)
                if default is None:
                    d = _DEFAULT_AFTER.match(b.text)
                    if d or re.search(r"\(\s*default\s*\)", b.text, re.I):
                        default = tok
                continue
            texts.append(b.text)

        for text in texts:
            found, single = _explicit_values(text)
            explicit += found
            singles += single
            loose += _code_values(text)
            if default is None:
                default = _default_in(text)

        options = _filter(explicit, own_names, explicit=True)
        # A lone "set to X" is often about another key; trust singles when several agree
        # with each other, or when they're code-formatted next to an explicit list.
        pool = _filter([v for v, _ in singles], own_names, explicit=True)
        if len(pool) < 2:
            pool = _filter([v for v, marked in singles if marked], own_names, explicit=True) if options else []
        seen = {o.lower() for o in options}
        options += [o for o in pool if o.lower() not in seen]
        if leading_default is not None or default is None:
            default = leading_default or pre_default
        # Code spans in a switch's or a number's description name other things, not values.
        plain_default = (default or "").lower()
        if not options and plain_default not in {"true", "false", "yes", "no", "on", "off"} and (
            not _NUMBER.match(plain_default)
        ):
            options = _filter(loose, other_keys, explicit=False)
        return DocsHit(options=options, excerpt=_excerpt(blocks[i:end]), source=self.source,
                       default=default)

    # -- prose fallback
    def _prose_hit(self, low: str) -> DocsHit | None:
        pattern = re.compile(rf"(?<![\w.-]){re.escape(low)}(?![\w-]|\.\w)", re.I)
        sentences = self._index.sentences
        exclude = (self._index.known - {low}) | {low}
        per_match: list[tuple[int, list[str]]] = []
        default: str | None = None
        for n, (sent, sent_low) in enumerate(sentences):
            if low not in sent_low or not pattern.search(sent):
                continue
            text = sent
            if n + 1 < len(sentences) and _CONTINUATION.match(sentences[n + 1][0]):
                text += " " + sentences[n + 1][0]
            found, _ = _explicit_values(text)
            for m in re.finditer(rf"(?<![\w.-]){re.escape(low)}\s*[:=]\s*([\w./+@:-]{{1,40}})", text, re.I):
                found.append(m.group(1))
            vals = _filter(found, {low}, explicit=True) or _filter(_code_values(text), exclude, False)
            if default is None:
                default = _default_in(text)
            if vals:
                per_match.append((n, vals))
        if not per_match and default is None:
            return None
        options: dict[str, str] = {}
        for _, vals in per_match:
            for v in vals:
                options.setdefault(v.lower(), v)
        best = max(per_match, key=lambda m: len(m[1]))[0] if per_match else next(
            n for n, (s, sl) in enumerate(sentences) if low in sl and pattern.search(s))
        window = sentences[max(0, best - 1): best + 2]
        excerpt = _truncate(" ".join(s.replace("`", "") for s, _ in window))
        return DocsHit(options=list(options.values()), excerpt=excerpt, source=self.source,
                       default=default)


def _excerpt(blocks: list[_Block]) -> str:
    parts: list[str] = []
    size = 0
    for b in blocks:
        text = b.text if b.kind == "pre" else b.text.replace("`", "")
        if b.kind == "li":
            text = "- " + text
        elif b.kind == "row":
            text = " | ".join(c.replace("`", "") for c in b.cells or [])
        parts.append(text)
        size += len(text) + 1
        if size > _EXCERPT_MAX:
            break
    return _truncate("\n".join(parts))


def _truncate(text: str) -> str:
    text = text.strip()
    if len(text) <= _EXCERPT_MAX:
        return text
    cut = text.rfind(" ", 0, _EXCERPT_MAX)
    return text[: cut if cut > _EXCERPT_MAX // 2 else _EXCERPT_MAX].rstrip() + "…"


# ---------------------------------------------------------------------------
# Loading

_META_REFRESH_RE = re.compile(
    r"""<meta\s+http-equiv\s*=\s*['"]?refresh['"]?\s+content\s*=\s*['"]?\s*\d+\s*;\s*url\s*=\s*([^'">\s]+)""",
    re.IGNORECASE,
)
_GITHUB_BLOB = re.compile(r"^https?://github\.com/([^/]+)/([^/]+)/blob/(.+)$", re.I)
_SAMPLE_EXTS = {".conf", ".cfg", ".ini", ".toml", ".yaml", ".yml", ".rc", ".config", ".properties"}
_MD_EXTS = {".md", ".markdown", ".mdx", ".mkd"}
_TEXT_EXTS = {".rst", ".txt", ".text", ".adoc"}


def load(source: str) -> DocsPage | None:
    """Parse a docs source; None when it can't be read or holds nothing usable."""
    try:
        return _load(source.strip())
    except Exception:
        return None


def _load(source: str) -> DocsPage | None:
    if source.startswith("man:"):
        text = _man(source[4:].strip())
        return _page(source, _parse_text(text)) if text else None
    if re.match(r"https?://", source, re.I):
        return _load_url(source)
    path = Path(source).expanduser()
    text = path.read_text(encoding="utf-8", errors="replace")
    return _page(source, _parse(text, path.name))


def _page(source: str, blocks: list[_Block]) -> DocsPage | None:
    return DocsPage(source, blocks) if blocks else None


def _github_raw(url: str) -> str | None:
    m = _GITHUB_BLOB.match(url.split("#", 1)[0].split("?", 1)[0])
    if not m:
        return None
    owner, repo, rest = m.groups()
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{rest}"


def _load_url(url: str) -> DocsPage | None:
    raw = _github_raw(url)
    resp = net.get(raw) if raw else None
    if resp is not None:
        return _page(url, _parse(resp.text, urlsplit(raw or url).path))
    resp = net.get(url)
    if resp is None:
        return None
    text, final = resp.text, resp.url
    for _ in range(2):
        if len(text) >= 4_000:
            break
        m = _META_REFRESH_RE.search(text)
        if not m:
            break
        nxt = net.get(urljoin(final, m.group(1)))
        if nxt is None:
            return None
        text, final = nxt.text, nxt.url
    return _page(url if raw else final, _parse(text, urlsplit(final).path))


def _man(name: str) -> str | None:
    m = re.fullmatch(r"([\w.+-]+)(?:\((\w+)\))?", name)
    if not m:
        return None
    args = ["man", m.group(2), m.group(1)] if m.group(2) else ["man", m.group(1)]
    env = {**os.environ, "MANPAGER": "cat", "PAGER": "cat", "MANWIDTH": "200"}
    proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=15)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", proc.stdout)
    return re.sub(r".\x08", "", text)


def _parse(text: str, name: str) -> list[_Block]:
    ext = Path(name).suffix.lower()
    head = text[:4000].lstrip().lower()
    if ext in (".html", ".htm", ".xhtml") or head.startswith(("<!doctype html", "<html")) or re.search(
        r"<(?:html|head|body)\b", head
    ):
        return _parse_html(text)
    if ext in _MD_EXTS:
        return _parse_markdown(text)
    if ext in _TEXT_EXTS:
        return _parse_text(text)
    if ext in _SAMPLE_EXTS or Path(name).name.endswith(("rc", "_config")):
        return _parse_sample(text)
    return _parse(text, "x" + _sniff(text))


def _sniff(text: str) -> str:
    lines = text.split("\n")
    md = sum(1 for ln in lines if _MD_FENCE.match(ln) or "](" in ln or re.match(r"\s*[-*+]\s+`", ln))
    settings = sum(1 for ln in lines if _SETTING.match(ln) or re.match(rf"^[#;]\s*{_KEY}\s*=", ln))
    if md >= 2 and md >= settings:
        return ".md"
    if settings >= 3:
        return ".conf"
    return ".txt"
