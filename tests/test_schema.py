import json
from pathlib import Path

from ezconfig import formats, schema


def make(tmp_path: Path, root: dict, name: str = "schema.json") -> schema.Schema:
    path = tmp_path / name
    path.write_text(json.dumps(root))
    loaded = schema.load(str(path), "schema")
    assert loaded is not None
    return loaded


def spec_at(s: schema.Schema, *path: str):
    hit = schema.walk(s, path)
    assert hit is not None
    return schema.spec_for(*hit)


def test_options_from_const_branches_are_strict(tmp_path):
    s = make(tmp_path, {"properties": {"mode": {"oneOf": [
        {"const": "light", "description": "Light colors"},
        {"const": "dark", "description": "Dark colors"},
    ]}}})
    spec = spec_at(s, "mode")
    assert (spec.widget, spec.options, spec.strict) == ("choice", ["light", "dark"], True)
    assert spec.option_notes == ["Light colors", "Dark colors"]


def test_open_branch_allows_other_values(tmp_path):
    s = make(tmp_path, {"properties": {"font": {"anyOf": [{"enum": ["mono", "sans"]}, {"type": "string"}]}}})
    spec = spec_at(s, "font")
    assert spec.options == ["mono", "sans"] and spec.strict is False and spec.warning


def test_list_items_and_relative_refs_across_files(tmp_path):
    (tmp_path / "defs.json").write_text(json.dumps({"definitions": {"port": {"type": "integer", "maximum": 65535}}}))
    s = make(tmp_path, {"properties": {"servers": {"type": "array", "items": {
        "type": "object", "properties": {"port": {"$ref": "defs.json#/definitions/port"}},
    }}}})
    spec = spec_at(s, "servers", "0", "port")
    assert spec.widget == "number" and spec.integer and spec.maximum == 65535


def test_declared_keys_lists_unset_keys_and_stops_on_recursion(tmp_path):
    s = make(tmp_path, {
        "definitions": {"node": {"type": "object", "properties": {"name": {"type": "string"}, "child": {"$ref": "#/definitions/node"}}}},
        "properties": {
            "tree": {"$ref": "#/definitions/node"},
            "env": {"type": "object", "additionalProperties": {"type": "string"}},
            "old": {"type": "boolean", "deprecated": True},
        },
    })
    d = schema.declared_keys(s)
    assert ("tree", "name") in d.leaves and ("tree", "child", "child") not in d.sections
    assert ("env",) in d.open_sections
    assert d.leaves[("old",)].deprecated


def test_declared_schema_comment_in_yaml(tmp_path):
    path = tmp_path / "a.yaml"
    path.write_text("# yaml-language-server: $schema=./s.json\na: 1\n")
    assert schema.declared(formats.load(path)) == "./s.json"


def test_catalog_match_uses_path_patterns(tmp_path, monkeypatch):
    catalog = {"schemas": [
        {"url": "https://x/workflow.json", "fileMatch": [".github/workflows/*.yml"]},
        {"url": "https://x/other.json", "fileMatch": ["other.yml"]},
    ]}
    monkeypatch.setattr(schema.net, "get_json", lambda url, timeout=8: catalog)
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    assert schema.catalog_match(workflow) == "https://x/workflow.json"
    assert schema.catalog_match(tmp_path / "ci.yml") is None


def test_validate_reports_key_paths(tmp_path):
    s = make(tmp_path, {"properties": {"port": {"type": "integer"}, "mode": {"enum": ["a", "b"]}}})
    problems = schema.validate({"port": "x", "mode": "c"}, s)
    assert len(problems) == 2
    assert any(p.startswith("port:") for p in problems) and any(p.startswith("mode:") for p in problems)
