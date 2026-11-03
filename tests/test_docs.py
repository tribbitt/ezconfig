import subprocess

import pytest

from ezconfig import docs, net

HTML = """<!doctype html>
<html><body>
<nav><ul><li><code>cursor-blink</code></li></ul></nav>
<main>
<h2><code>cursor-style</code></h2>
<p>The style of the cursor.</p>
<p>Valid values are:</p>
<ul>
  <li><code>block</code></li>
  <li><code>bar</code> - a thin vertical bar</li>
  <li><code>underline</code></li>
</ul>
<p>Default: <code>block</code></p>
<h2><code>window-theme</code></h2>
<p>The window theme. Can be one of <code>auto</code>, <code>light</code> or <code>dark</code>.</p>
<dl><dt>font_size</dt><dd><p>Font size in points. The default is 12.</p></dd></dl>
<h2>Other settings</h2>
<p>The bell setting accepts one of <code>audio</code>, <code>visual</code>, or <code>none</code>.</p>
<table>
  <thead><tr><th>Option</th><th>Values</th><th>Default</th></tr></thead>
  <tr><td><code>scrollbar</code></td><td><code>always</code> | <code>never</code> | <code>auto</code></td><td><code>auto</code></td></tr>
</table>
</main>
<footer><p><code>footer-key</code>: not documentation</p></footer>
</body></html>
"""

MARKDOWN = """# Options

Here are the available options:

* `clipboard`: specifies how to access the system clipboard.
   Possible values are:
    * `external`: accesses the clipboard via an external tool.
    * `internal`: uses an internal clipboard.

    default value: `external`

* `fileformat`: the line endings to use. The two possible values for this option
   are `unix` and `dos`.

    default value: `unix`

## Colors

| Key | Type | Values | Default |
|-----|------|--------|---------|
| `colorscheme` | string | `monokai`, `solarized`, `zenburn` | `monokai` |

```toml
tabsize = 4
```
"""

MAN = """SSH_CONFIG(5)                File Formats Manual               SSH_CONFIG(5)

N\bNA\bAM\bME\bE
     ssh_config \u2013 OpenSSH client configuration file

D\bDE\bES\bSC\bCR\bRI\bIP\bPT\bTI\bIO\bON\bN
     The possible keywords and their meanings are as follows.

     C\bCo\bom\bmp\bpr\bre\bes\bss\bsi\bio\bon\bn
             Specifies whether to use compression.  The argument must be yes or no
             (the default).

     LogLevel
             Gives the verbosity level.  The possible values are: QUIET, FATAL,
             ERROR, INFO, VERBOSE, DEBUG.  The default is INFO.

     StrictHostKeyChecking
             If this flag is set to yes, ssh will never add host keys.  If this flag
             is set to accept-new then ssh adds new keys.  If this flag is set to no
             or off, ssh adds them.  If this flag is set to ask (the default), new
             keys are confirmed.
"""

SAMPLE = """# Sample configuration for an example app.

# The cursor shape. One of block, beam or underline.
cursor_shape block

# Font size in points.
#font_size = 11.0

# How the window is decorated.
# Valid values: full, none, transparent
decorations = full
"""


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.fixture
def html_page(tmp_path):
    page = docs.load(_write(tmp_path, "reference.html", HTML))
    assert page is not None
    return page


def test_html_heading_section_with_bullet_values(html_page):
    hit = html_page.lookup("cursor-style")
    assert hit.options == ["block", "bar", "underline"]
    assert hit.default == "block"
    assert "style of the cursor" in hit.excerpt
    assert hit.source == html_page.source
    assert html_page.lookup("CURSOR-STYLE") == hit


def test_html_prose_list_does_not_bleed_into_neighbours(html_page):
    theme = html_page.lookup("window-theme")
    assert theme.options == ["auto", "light", "dark"]
    assert "block" not in theme.options
    assert "auto" not in html_page.lookup("cursor-style").options


def test_html_definition_term_and_table_row(html_page):
    font = html_page.lookup("font_size")
    assert font.options == []
    assert font.default == "12"
    scrollbar = html_page.lookup("scrollbar")
    assert scrollbar.options == ["always", "never", "auto"]
    assert scrollbar.default == "auto"


def test_html_prose_fallback(html_page):
    bell = html_page.lookup("bell")
    assert bell is not None
    assert bell.options == ["audio", "visual", "none"]
    assert html_page.lookup("no-such-key") is None


def test_html_discover_keys_skips_chrome_and_values(html_page):
    assert set(html_page.discover_keys()) == {"cursor-style", "window-theme", "font_size", "scrollbar"}


def test_markdown_bullets_and_tables(tmp_path):
    page = docs.load(_write(tmp_path, "options.md", MARKDOWN))
    clipboard = page.lookup("clipboard")
    assert clipboard.options == ["external", "internal"]
    assert clipboard.default == "external"
    fileformat = page.lookup("fileformat")
    assert fileformat.options == ["unix", "dos"]
    assert fileformat.default == "unix"
    colors = page.lookup("colorscheme")
    assert colors.options == ["monokai", "solarized", "zenburn"]
    assert colors.default == "monokai"
    assert set(page.discover_keys()) == {"clipboard", "fileformat", "colorscheme"}


def test_man_page(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs["env"]["MANPAGER"], kwargs["env"]["MANWIDTH"]))
        return subprocess.CompletedProcess(args, 0, stdout=MAN, stderr="")

    monkeypatch.setattr(docs.subprocess, "run", fake_run)
    page = docs.load("man:ssh_config")
    assert calls == [(["man", "ssh_config"], "cat", "200")]
    assert page.source == "man:ssh_config"

    compression = page.lookup("compression")
    assert compression.options == []  # yes/no is a switch, not a dropdown
    assert compression.default == "no"
    loglevel = page.lookup("LogLevel")
    assert loglevel.options == ["QUIET", "FATAL", "ERROR", "INFO", "VERBOSE", "DEBUG"]
    assert loglevel.default == "INFO"
    strict = page.lookup("StrictHostKeyChecking")
    assert strict.options == ["yes", "accept-new", "no", "ask"]
    assert strict.default == "ask"
    assert set(page.discover_keys()) == {"Compression", "LogLevel", "StrictHostKeyChecking"}


def test_man_page_failure(monkeypatch):
    monkeypatch.setattr(
        docs.subprocess, "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 16, stdout="", stderr="No entry"),
    )
    assert docs.load("man:nonexistent") is None


def test_commented_sample_config(tmp_path):
    page = docs.load(_write(tmp_path, "app.conf", SAMPLE))
    cursor = page.lookup("cursor_shape")
    assert cursor.options == ["block", "beam", "underline"]
    assert cursor.default == "block"
    assert "cursor shape" in cursor.excerpt
    font = page.lookup("font_size")
    assert font.options == []
    assert font.default == "11.0"
    decorations = page.lookup("decorations")
    assert decorations.options == ["full", "none", "transparent"]
    assert decorations.default == "full"
    assert set(page.discover_keys()) == {"cursor_shape", "font_size", "decorations"}


def test_github_blob_is_fetched_raw(monkeypatch):
    requested = []

    def fake_get(url, timeout=8.0):
        requested.append(url)
        return net.Response(url=url, text=MARKDOWN)

    monkeypatch.setattr(net, "get", fake_get)
    blob = "https://github.com/owner/repo/blob/main/docs/options.md"
    page = docs.load(blob)
    assert requested == ["https://raw.githubusercontent.com/owner/repo/main/docs/options.md"]
    assert page.source == blob
    assert page.lookup("fileformat").options == ["unix", "dos"]
    assert page.lookup("fileformat").source == blob


def test_meta_refresh_is_followed(monkeypatch):
    pages = {
        "https://old.example/docs": '<html><head><meta http-equiv="refresh" content="0; url=/new/"></head></html>',
        "https://old.example/new/": HTML,
    }
    monkeypatch.setattr(net, "get", lambda url, timeout=8.0: net.Response(url=url, text=pages[url]))
    page = docs.load("https://old.example/docs")
    assert page.source == "https://old.example/new/"
    assert page.lookup("cursor-style").options == ["block", "bar", "underline"]


def test_load_failures_return_none(monkeypatch, tmp_path):
    monkeypatch.setattr(net, "get", lambda url, timeout=8.0: None)
    assert docs.load("https://example.invalid/docs") is None
    assert docs.load(str(tmp_path / "missing.md")) is None
    assert docs.load("") is None
