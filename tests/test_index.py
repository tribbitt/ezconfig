import json

import pytest

from ezconfig import formats, schema
from ezconfig.index import build

DOCS = """# Options

## cursor-style

The cursor shape. Valid values are `block`, `bar`, and `underline`.

## option-as-alt

Use option as alt. Can be `true`, `false`, `left` or `right`.

## mode

One of `fast` or `slow`.

## window-theme

The theme. Valid values: `light`, `dark`. Default: `light`.
"""


@pytest.fixture(autouse=True)
def no_catalog(monkeypatch):
    monkeypatch.setattr(schema, "catalog_match", lambda path: None)


def test_docs_options_unset_keys_and_schema_precedence(tmp_path):
    (tmp_path / "docs.md").write_text(DOCS)
    (tmp_path / "schema.json").write_text(json.dumps({"properties": {"mode": {"enum": ["a", "b"]}}}))
    config = tmp_path / "app.json"
    config.write_text('{"cursor-style": "beam", "option-as-alt": true, "mode": "a"}')
    index = build(formats.load(config), [str(tmp_path / "docs.md")], str(tmp_path / "schema.json"))

    cursor = index.specs[("cursor-style",)]
    assert cursor.widget == "choice" and cursor.options == ["beam", "block", "bar", "underline"]
    assert cursor.source.startswith("docs") and not cursor.strict

    alt = index.specs[("option-as-alt",)]
    assert alt.widget == "choice" and alt.options == [True, False, "left", "right"]

    mode = index.specs[("mode",)]  # a strict schema enum wins over docs
    assert mode.options == ["a", "b"] and mode.strict

    theme = index.unset[("window-theme",)]
    assert theme.options == ["light", "dark"] and theme.default == "light"


def test_theme_files_join_schema_options(tmp_path):
    (tmp_path / "colorschemes").mkdir()
    (tmp_path / "colorschemes" / "mine.micro").write_text("")
    (tmp_path / "schema.json").write_text(json.dumps({"properties": {"colorscheme": {"enum": ["default"]}}}))
    config = tmp_path / "settings.json"
    config.write_text('{"colorscheme": "mine"}')
    index = build(formats.load(config), [], str(tmp_path / "schema.json"))
    assert index.specs[("colorscheme",)].options == ["default", "mine"]
