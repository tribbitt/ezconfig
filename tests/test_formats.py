import json
from pathlib import Path

import pytest

from ezconfig import document, formats
from ezconfig.text_format import TextDocument


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


# --- JSON / JSONC ------------------------------------------------------------

JSONC = """{
  // editor settings
  "editor": {
    "fontSize": 12, /* points */
    "theme": "dark",
  },
  "files": ["a", "b"],
}
"""


def test_jsonc_set_keeps_comments_and_trailing_commas(tmp_path):
    doc = formats.load(write(tmp_path, "settings.jsonc", JSONC))
    doc.set(("editor", "fontSize"), 14)
    doc.set(("files",), ["a", "b", "c"])
    out = doc.dumps()
    assert "// editor settings" in out and "/* points */" in out
    assert '"fontSize": 14, /* points */' in out
    assert '"files": ["a", "b", "c"],' in out


def test_jsonc_add_nested_creates_parents_and_parses(tmp_path):
    doc = formats.load(write(tmp_path, "settings.json", JSONC))
    created = doc.add(("ui", "panel", "width"), 30)
    assert created == [("ui",), ("ui", "panel")]
    doc.add(("editor", "wrap"), True)
    reparsed = formats.load(write(tmp_path, "again.json", doc.dumps()))
    assert reparsed.data["ui"] == {"panel": {"width": 30}}
    assert reparsed.data["editor"]["wrap"] is True
    assert "// editor settings" in doc.dumps()


def test_json_delete_last_member_keeps_strict_json_valid(tmp_path):
    doc = formats.load(write(tmp_path, "a.json", '{\n  "a": 1,\n  "b": 2\n}\n'))
    doc.delete(("b",))
    assert json.loads(doc.dumps()) == {"a": 1}
    doc.add(("c",), "x")
    assert json.loads(doc.dumps()) == {"a": 1, "c": "x"}


def test_json_add_to_empty_and_inline_objects(tmp_path):
    doc = formats.load(write(tmp_path, "a.json", '{"inline": {"x": 1}, "empty": {}}'))
    doc.add(("inline", "y"), 2)
    doc.add(("empty", "z"), [1])
    assert json.loads(doc.dumps()) == {"inline": {"x": 1, "y": 2}, "empty": {"z": [1]}}


def test_json_list_items_are_keys(tmp_path):
    doc = formats.load(write(tmp_path, "a.json", '{"servers": [{"host": "a"}, {"host": "b"}]}'))
    assert (("servers", "1", "host"), "b") in list(doc.walk())
    doc.set(("servers", "1", "host"), "c")
    assert json.loads(doc.dumps())["servers"][1]["host"] == "c"


def test_json_reports_line_of_syntax_error(tmp_path):
    with pytest.raises(ValueError, match="line 3"):
        formats.load(write(tmp_path, "bad.json", '{\n  "a": 1\n  "b": 2\n}'))


# --- INI -----------------------------------------------------------------------

INI = """; top comment
[core]
editor = vim
autocrlf = yes

[install]
requires =
    foo
    bar
"""


def test_ini_edits_keep_comments_and_bool_words(tmp_path):
    doc = formats.load(write(tmp_path, "app.ini", INI))
    assert doc.data["core"]["autocrlf"] is True
    assert doc.data["install"]["requires"] == ["foo", "bar"]
    doc.set(("core", "autocrlf"), False)
    doc.set(("install", "requires"), ["foo"])
    out = doc.dumps()
    assert out.startswith("; top comment\n[core]\n")
    assert "autocrlf = no\n" in out
    assert "requires =\n    foo\n" in out and "bar" not in out


def test_ini_add_into_section_and_new_section(tmp_path):
    doc = formats.load(write(tmp_path, "app.ini", INI))
    assert doc.add(("core", "color"), "auto") == []
    assert doc.add(("ui", "theme"), "dark") == [("ui",)]
    reparsed = formats.load(write(tmp_path, "b.ini", doc.dumps()))
    assert reparsed.data["core"]["color"] == "auto"
    assert reparsed.data["ui"] == {"theme": "dark"}
    lines = doc.dumps().splitlines()
    assert lines.index("color = auto") < lines.index("[install]")


def test_ini_delete_section(tmp_path):
    doc = formats.load(write(tmp_path, "app.ini", INI))
    doc.delete(("core",))
    assert "editor" not in doc.dumps() and "[install]" in doc.dumps()


def test_ini_indented_entries_like_gitconfig(tmp_path):
    doc = formats.load(write(tmp_path, ".gitconfig", '[user]\n\tname = A\n\temail = a@b\n[remote "origin"]\n\turl = x\n'))
    assert doc.data == {"user": {"name": "A", "email": "a@b"}, 'remote "origin"': {"url": "x"}}
    doc.add(("user", "signingkey"), "K")
    assert "\temail = a@b\n\tsigningkey = K\n[remote" in doc.dumps()


def test_conf_without_sections_uses_text_format(tmp_path):
    doc = formats.load(write(tmp_path, "kitty.conf", "font_size 12.0\ncursor_shape beam\n"))
    assert isinstance(doc, TextDocument)
    assert doc.data["cursor_shape"] == "beam"


# --- YAML / TOML ---------------------------------------------------------------


def test_yaml_keeps_indentation_and_quotes(tmp_path):
    text = 'server:\n    host: "localhost"\n    ports:\n      - 80\n      - 443\n'
    doc = formats.load(write(tmp_path, "a.yaml", text))
    doc.set(("server", "host"), "example.org")
    assert doc.dumps() == text.replace("localhost", "example.org")


def test_toml_array_of_tables_is_navigable(tmp_path):
    doc = formats.load(write(tmp_path, "a.toml", "[[bin]]\nname = 'a'\n\n[[bin]]\nname = 'b'\n"))
    assert [p for p, _ in doc.walk()] == [("bin", "0", "name"), ("bin", "1", "name")]
    doc.set(("bin", "1", "name"), "c")
    assert "name = 'a'" in doc.dumps() and 'name = "c"' in doc.dumps()


def test_unknown_extension_is_sniffed(tmp_path):
    assert formats.load(write(tmp_path, "rc", '{"a": 1}')).format_name == "json"
    assert formats.load(write(tmp_path, "rc2", "a = 1\n[t]\nb = 'x'\n")).format_name == "toml"
    assert formats.load(write(tmp_path, "rc3", "a: 1\nb: [x]\n")).format_name == "yaml"


# --- Text format ---------------------------------------------------------------

GHOSTTY = """# theme
background = #282c34
font-family = JetBrains Mono  # main font
palette = 0=#1d1f21
palette = 1=#cc6666
cursor-style-blink = true
"""


def test_text_edit_changes_only_the_value_span(tmp_path):
    doc = formats.load(write(tmp_path, "config", GHOSTTY))
    assert isinstance(doc, TextDocument)
    assert doc.data["background"] == "#282c34"
    assert doc.data["font-family"] == "JetBrains Mono"
    doc.set(("font-family",), "Iosevka")
    doc.set(("cursor-style-blink",), False)
    assert doc.dumps() == GHOSTTY.replace("JetBrains Mono", "Iosevka").replace("blink = true", "blink = false")


def test_text_keys_are_unique_and_named_by_assignment(tmp_path):
    doc = formats.load(write(tmp_path, "config", GHOSTTY))
    palette = [p for p, _ in doc.walk() if doc.name_of(p) == "palette"]
    assert len(palette) == len(set(palette)) == 4  # two lines, a number and a color on each


def test_text_add_and_delete_lines(tmp_path):
    doc = formats.load(write(tmp_path, "config", GHOSTTY))
    doc.add(("window-theme",), "dark")
    assert doc.dumps().endswith("cursor-style-blink = true\nwindow-theme = dark\n")
    assert doc.data["window-theme"] == "dark"
    doc.delete(("background",))
    assert "background" not in doc.dumps()


def test_text_without_assignments_cannot_add(tmp_path):
    doc = formats.load(write(tmp_path, "theme.micro", 'color-link comment "#75715E"\n'))
    assert doc.can_add is False
    assert doc.data == {"comment": "#75715E"}


# --- Saving --------------------------------------------------------------------


def test_save_follows_symlink_and_backs_up_once(tmp_path, monkeypatch):
    monkeypatch.setattr(document, "BACKUP_ROOT", tmp_path / "backups")
    real = write(tmp_path, "real.toml", "a = 1\n")
    link = tmp_path / "myapp" / "config.toml"
    link.parent.mkdir()
    link.symlink_to(real)
    doc = formats.load(link)
    doc.set(("a",), 2)
    copy = doc.save()
    doc.set(("a",), 3)
    doc.save()
    assert link.is_symlink() and real.read_text() == "a = 3\n"
    assert copy == tmp_path / "backups" / "real config" / "real.toml"
    assert copy.read_text() == "a = 1\n"
    assert (copy.parent / "info.txt").read_text().count("saved") == 1


def test_parse_literal():
    assert document.parse_literal("true") is True
    assert document.parse_literal("42") == 42
    assert document.parse_literal("1.5") == 1.5
    assert document.parse_literal('"42"') == "42"
    assert document.parse_literal("dark") == "dark"
