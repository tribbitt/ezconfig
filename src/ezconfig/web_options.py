"""Discover candidate values for text-typed config keys from a docs URL.

Strategy: fetch the page once, flatten HTML to plain text (preserving inline
code as backtick-quoted spans), split into sentences, then for each key the
caller asks about find sentences that mention the key and pull plausible
value tokens out of those sentences and their immediate neighbors.

Confidence is low by design: the docs may use different terminology than the
user's file, value lists may be partial, and a sentence-bag scraper can't
distinguish "valid value" from "example value". The schema cascade surfaces
that uncertainty as a warning, and the matched excerpt (preceding + matched
+ following sentence) is shown alongside so the user can verify in context.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

import requests


_SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "svg", "iframe", "form"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "li", "ul", "ol", "tr", "td", "th",
    "h1", "h2", "h3", "h4", "h5", "h6", "br", "pre", "blockquote", "dt", "dd",
    "thead", "tbody", "hr",
}
_CODE_TAGS = {"code", "tt", "kbd", "samp", "var"}
# When the document declares a main-content container, throw away everything
# outside it. Catches the GitHub "code blob" chrome (logo, repo banner, file
# tree, footer) that otherwise pollutes the line stream with repo/user names,
# branch dropdowns, etc., as if they were config keys.
_MAIN_TAGS = {"main", "article"}
_MAIN_CLASS_HINTS = ("markdown-body", "Box-body", "container--main", "content")


class _Extractor(HTMLParser):
    """Flatten HTML → plain text. Wraps inline-code spans in literal backticks
    so value detection can use a single regex regardless of whether the docs
    were rendered from markdown or hand-written HTML.

    If the page declares a main-content container (<main>, <article>, or a div
    whose class hints at the rendered-content area), the extractor pivots into
    "content-only" mode at first hit: previously-buffered tokens (typically
    page chrome) are discarded, and anything outside subsequent main containers
    is skipped. Pages without such a container fall back to the whole body.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._buf: list[str] = []
        # Main-content gating
        self._main_depth = 0
        self._saw_main = False  # latched: once True, only main-depth content counts

    def _is_main(self, tag: str, attrs) -> bool:
        if tag in _MAIN_TAGS:
            return True
        if tag != "div":
            return False
        cls = next((v for k, v in attrs if k == "class" and v), "") or ""
        return any(h in cls for h in _MAIN_CLASS_HINTS)

    def _accepting(self) -> bool:
        if self._skip_depth > 0:
            return False
        if self._saw_main and self._main_depth == 0:
            return False
        return True

    def handle_starttag(self, tag: str, attrs):  # type: ignore[override]
        if self._is_main(tag, attrs):
            if not self._saw_main:
                # First main container — drop any chrome we accumulated.
                self._saw_main = True
                self._buf = []
            self._main_depth += 1
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if not self._accepting():
            return
        if tag in _CODE_TAGS:
            self._buf.append("`")
        if tag in _BLOCK_TAGS:
            self._buf.append("\n")

    def handle_startendtag(self, tag: str, attrs):  # type: ignore[override]
        if not self._accepting():
            return
        if tag in _BLOCK_TAGS:
            self._buf.append("\n")

    def handle_endtag(self, tag: str):  # type: ignore[override]
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if not self._accepting():
            if tag in _MAIN_TAGS or tag == "div":
                # Could be the closing of the main container — fall through.
                pass
            else:
                return
        if tag in _CODE_TAGS and self._accepting():
            self._buf.append("`")
        if tag in _BLOCK_TAGS and self._accepting():
            self._buf.append("\n")
        # Track main-depth on close. We don't know the original tag from attrs,
        # so just decrement on any tag in _MAIN_TAGS or div; harmless because
        # main_depth bottoms at 0.
        if tag in _MAIN_TAGS:
            self._main_depth = max(0, self._main_depth - 1)
        # For divs we can't reliably match opener/closer without attribute
        # memory, so leave main_depth alone for div ends — the next main-tag
        # close will resync. This is a coarse approximation but fine in
        # practice: GitHub pages have a single top-level markdown-body div.

    def handle_data(self, data: str) -> None:
        if not self._accepting():
            return
        self._buf.append(data)

    def get_text(self) -> str:
        return "".join(self._buf)


_INLINE_WS = re.compile(r"[ \t]+")
# Sentences that start with one of these phrases are treated as a continuation
# of the preceding key mention — docs frequently split "The X setting controls
# Y. Valid values are A, B, C." across two sentences. The list is deliberately
# narrow so unrelated neighbours don't get scooped up.
_CONTINUATION_RE = re.compile(
    r"^\s*("
    r"valid\s+values|allowed\s+values|possible\s+values|supported\s+values|"
    r"accepted\s+values|available\s+values|"
    r"options\b|one\s+of\b|must\s+be\s+one\s+of|must\s+be\b|"
    r"can\s+be\b|may\s+be\b|it\s+accepts\b|accepts\s+one\s+of|accepts\b|"
    r"the\s+values\b|set\s+to\b"
    r")",
    re.IGNORECASE,
)
# A defensible enum value: short, no whitespace, alphanumeric + a few separators.
_VAL_TOKEN = re.compile(r"^[A-Za-z0-9_./+@:\-]{1,40}$")
# Words we never want to surface as enum candidates — they sneak in from prose
# (e.g. "the default is `true`") and would pollute string-typed dropdowns.
_NOISE = {"true", "false", "null", "none", "yes", "no", "default", "example", "e.g", "i.e"}


def _flatten(html_str: str) -> str:
    p = _Extractor()
    p.feed(html_str)
    return p.get_text()


def _block_lines(text: str) -> list[str]:
    """Block-level lines from the flattened text — preserves the structural
    boundaries the HTML block tags created. Used by key-discovery to spot
    identifiers in heading / definition-term position."""
    out: list[str] = []
    for line in text.split("\n"):
        line = _INLINE_WS.sub(" ", line).strip()
        if line:
            out.append(line)
    return out


def _sentences_from_lines(lines: list[str]) -> list[str]:
    """Sentence-split each block line on `.!?` followed by whitespace + a
    capital-ish start character. Block boundaries are sentence boundaries."""
    out: list[str] = []
    for line in lines:
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(`'\"\[])", line)
        for part in parts:
            part = part.strip()
            if part:
                out.append(part)
    return out


def _extract_values(sentence: str, key: str) -> list[str]:
    """Pull plausible value tokens out of a sentence that mentions `key`."""
    found: list[str] = []
    key_lower = key.lower()

    # 1) Backtick-quoted spans — covers both <code> tags (we wrapped them in
    #    backticks during flattening) and any literal backticks in the source.
    for m in re.finditer(r"`([^`\n]{1,40})`", sentence):
        tok = m.group(1).strip().strip(":=,;.")
        if tok and _VAL_TOKEN.match(tok):
            found.append(tok)

    # 2) Double / single-quoted strings (docs that don't use code formatting).
    for m in re.finditer(r'"([^"\n]{1,40})"|\'([^\'\n]{1,40})\'', sentence):
        tok = (m.group(1) or m.group(2) or "").strip().strip(":=,;.")
        if tok and _VAL_TOKEN.match(tok):
            found.append(tok)

    # 3) `key: value` / `key = value` patterns appearing in prose.
    for m in re.finditer(
        rf"(?:^|\W){re.escape(key)}\s*[:=]\s*([A-Za-z0-9_./+@:\-]{{1,40}})",
        sentence,
        re.IGNORECASE,
    ):
        tok = m.group(1).strip().strip(",;.")
        if tok and _VAL_TOKEN.match(tok):
            found.append(tok)

    # Dedupe (preserve first-seen order) and drop noise.
    seen: dict[str, None] = {}
    for v in found:
        vl = v.lower()
        if vl == key_lower or vl in _NOISE:
            continue
        # Pure numerics are almost always counts or ports, not enum values —
        # and the field would be a number widget anyway if it were numeric.
        if v.lstrip("-").replace(".", "", 1).isdigit():
            continue
        seen.setdefault(v, None)
    return list(seen)


@dataclass
class WebOption:
    """Discovered values for one key, plus an excerpt that grounds the source."""
    values: list[str]
    excerpt: str
    url: str


class WebPage:
    """Parsed docs page, queried per-key by lookup() and en masse by discover_keys()."""

    def __init__(self, url: str, sentences: list[str], lines: list[str] | None = None) -> None:
        self.url = url
        self.sentences = sentences
        self.lines = lines if lines is not None else sentences

    def lookup(self, key: str) -> WebOption | None:
        if not key or not self.sentences:
            return None
        key_re = re.compile(rf"(?<!\w){re.escape(key)}(?!\w)", re.IGNORECASE)
        per_match: list[tuple[int, list[str]]] = []  # (sentence_idx, values_in_sentence)
        # Values are extracted from the matched sentence only — neighbours are
        # used purely for the excerpt window. Pulling values from neighbours
        # bleeds tokens from unrelated nearby keys (e.g. discovering 'host' next
        # to a 'title' mention) which produced wrong dropdowns in early tests.
        for i, sent in enumerate(self.sentences):
            if not key_re.search(sent):
                continue
            vals = _extract_values(sent, key)
            # Continuation: docs often phrase "The X setting controls Y. Valid
            # values are A, B, C." — peek to the next sentence only when it
            # opens with an explicit value-listing phrase.
            if i + 1 < len(self.sentences):
                nxt = self.sentences[i + 1]
                if _CONTINUATION_RE.match(nxt):
                    vals = vals + _extract_values(nxt, key)
            if vals:
                per_match.append((i, vals))
        if not per_match:
            return None

        # Pick the most informative match (most candidate values) for the excerpt.
        best_idx, _ = max(per_match, key=lambda m: len(m[1]))
        excerpt_window = self.sentences[max(0, best_idx - 1) : best_idx + 2]
        excerpt = " ".join(s.replace("`", "") for s in excerpt_window)
        # Aggregate values across every mention, preserving first-seen order.
        seen: dict[str, None] = {}
        for _, vs in per_match:
            for v in vs:
                seen.setdefault(v, None)
        return WebOption(values=list(seen), excerpt=excerpt, url=self.url)


    def discover_keys(self) -> dict[str, WebOption]:
        """Find identifiers the docs treat as config keys.

        A key candidate is a line whose first non-marker token is either a
        backtick-wrapped identifier or a multi-segment bare identifier
        (kebab/snake/dotted). Heading / definition-term / list-item structure
        produces exactly this pattern after HTML flattening, so it's a
        high-precision signal for "this is the name of a config option."

        Single-segment names must be 5+ chars to filter out common English
        words ("type", "name", "use", "set") that show up at the start of
        bullet lines in prose without being keys.
        """
        # Identifier shape — starts with a lowercase letter so we skip English
        # headings ("Default", "Linux"). Body chars include uppercase so we
        # catch camelCase keys like scrollHeight / confirmOnQuit. The optional
        # segments cover kebab-case, snake_case, and dotted paths.
        _IDENT = r"[a-z][A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*"
        _BACKTICK_RE = re.compile(rf"^`({_IDENT})`")
        _IDENT_RE = re.compile(rf"^({_IDENT})(?=[\s:=(,.\-—–]|$)")
        # An identifier alone on its line (e.g. `<li><a>font</a></li>` flattened
        # to "font", or a markdown `<h3>theme</h3>`). 4+ chars total — short
        # enough to catch `font`/`mode`/`size`/`port`, long enough to avoid
        # 2- and 3-letter prose tokens that occasionally appear standalone.
        _SINGLE_RE = re.compile(rf"^([a-z][A-Za-z0-9]{{3,}})\s*$")
        # Assignment-shaped line: `key = value` or `key: value`. Catches
        # man-page-style references (alacritty), YAML/TOML examples, and
        # markdown definition lists. Combined with the lowercase-start
        # requirement above, this is precise enough to keep prose like
        # "Default:" / "Linux:" / "Note:" out.
        _ASSIGN_RE = re.compile(rf"^({_IDENT})\s*[:=]")
        # Glossary-shape line: `ident (description text)`. Common in bullet
        # lists that name a thing followed by a parenthetical gloss — e.g.
        # micro's colorscheme groups: "default (color of the background ...)".
        # Requires a balanced paren on the same line so this doesn't fire on
        # prose with stray open-parens.
        _PAREN_DEF_RE = re.compile(rf"^([a-z][A-Za-z0-9]{{3,}})\s*\([^)]*\)")
        _CODE_FENCE_RE = re.compile(r"^\s*```")

        found: dict[str, WebOption] = {}
        in_code = False
        for line in self.lines:
            if _CODE_FENCE_RE.match(line):
                in_code = not in_code
                continue
            # Strip leading markdown markers (#, *, -, +, >, |). Bullet dashes
            # need a non-word char after them so we don't eat `-cli-flag`.
            body = re.sub(r"^(?:[#>*+|]|-(?!\w))+\s*", "", line).lstrip()
            m = _BACKTICK_RE.match(body) or _IDENT_RE.match(body)
            if m is None:
                continue
            key = m.group(1)
            has_seg = any(c in key for c in "-_.")
            # Single-segment bare names are dangerous (English prose contains
            # lots of 5+ char lowercase words). Require an explicit signal
            # that this token is a config key: backticks, alone on the line,
            # assignment syntax, or a code-block context.
            if not has_seg:
                strong_signal = (
                    _BACKTICK_RE.match(body)
                    or _SINGLE_RE.match(body)
                    or _ASSIGN_RE.match(body)
                    or _PAREN_DEF_RE.match(body)
                    or in_code
                )
                if not strong_signal:
                    continue
            if key in found:
                continue
            opt = self.lookup(key)
            found[key] = opt or WebOption(values=[], excerpt=line, url=self.url)
        return found


_META_REFRESH_RE = re.compile(
    r"""<meta\s+http-equiv\s*=\s*['"]?refresh['"]?\s+content\s*=\s*['"]?\s*\d+\s*;\s*url\s*=\s*([^'">\s]+)""",
    re.IGNORECASE,
)


def fetch_page(url: str, timeout: float = 8.0) -> WebPage | None:
    """Download `url` and return a parsed WebPage, or None on any failure.

    Failures here are non-fatal: the caller treats absence the same as "no
    web options discovered" so the rest of the schema cascade still works.
    Follows up to two meta-refresh redirects — common on docs sites that
    moved (e.g. atuin) and serve a redirect stub from the old path.
    """
    headers = {"User-Agent": "ezconfig/0.1 (+option-discovery)"}
    final_url = url
    text = ""
    for _ in range(3):
        try:
            resp = requests.get(final_url, timeout=timeout, headers=headers)
            resp.raise_for_status()
        except (requests.RequestException, OSError):
            return None
        text = resp.text
        final_url = resp.url
        # Detect a meta-refresh stub (small body, short HTML, refresh tag).
        if len(text) < 4_000:
            m = _META_REFRESH_RE.search(text)
            if m:
                target = m.group(1)
                final_url = requests.compat.urljoin(resp.url, target)
                continue
        break
    lines = _block_lines(_flatten(text))
    sentences = _sentences_from_lines(lines)
    return WebPage(url=final_url, sentences=sentences, lines=lines)
