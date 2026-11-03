"""JSON Schema: find the schema for a file, read key specs from it, list the
keys it declares, and validate data against it.

Where schemas come from, strongest first:
  1. --schema on the command line;
  2. a declaration in the file: `$schema` (JSON/YAML/TOML),
     `# yaml-language-server: $schema=URL`, or `#:schema URL` (TOML);
  3. the SchemaStore catalog, matched by file name and path.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from ezconfig import net
from ezconfig.document import Document, KeyPath
from ezconfig.spec import COLOR_RE, FieldSpec

CATALOG_URL = "https://www.schemastore.org/api/json/catalog.json"

# Branching keywords: which properties apply depends on other values, so
# their properties are not listed as unset keys.
_CONDITIONAL = ("oneOf", "anyOf", "if", "then", "else", "dependentSchemas")


@dataclass(frozen=True)
class Scope:
    """A schema document and the URL it was loaded from (for relative $refs)."""
    root: dict[str, Any]
    base: str


@dataclass
class Schema:
    scope: Scope
    label: str  # shown to the user, e.g. "schema (https://…)"


# --- Loading -----------------------------------------------------------------

_local_cache: dict[str, dict[str, Any] | None] = {}


def to_url(ref: str, relative_to: Path | None = None) -> str:
    """URL for a schema reference given as a URL or a file path."""
    if re.match(r"^[a-z][a-z0-9+.-]*://", ref, re.I):
        return ref
    path = Path(ref).expanduser()
    if not path.is_absolute() and relative_to is not None:
        path = relative_to / path
    return path.resolve().as_uri()


def fetch(url: str) -> dict[str, Any] | None:
    """Schema document at `url` (http(s) or file), or None."""
    url = url.split("#", 1)[0]
    if url.startswith(("http://", "https://")):
        data = net.get_json(url)
        return data if isinstance(data, dict) else None
    if url not in _local_cache:
        try:
            data = json.loads(Path(unquote(urlparse(url).path)).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        _local_cache[url] = data if isinstance(data, dict) else None
    return _local_cache[url]


def load(ref: str, label_prefix: str, relative_to: Path | None = None) -> Schema | None:
    url = to_url(ref, relative_to)
    root = fetch(url)
    if root is None:
        return None
    return Schema(Scope(root, url), f"{label_prefix} ({ref})")


_YAML_DIRECTIVE = re.compile(r"#\s*yaml-language-server:\s*\$schema=(\S+)")
_TOML_DIRECTIVE = re.compile(r"^#:schema\s+(\S+)", re.M)


def declared(doc: Document) -> str | None:
    """Schema reference declared inside the file, if any."""
    data = doc.data
    if isinstance(data, dict) and isinstance(data.get("$schema"), str):
        return data["$schema"]
    head = doc.original_text[:4000]
    for regex in (_YAML_DIRECTIVE, _TOML_DIRECTIVE):
        if m := regex.search(head):
            return m.group(1)
    return None


def catalog_match(path: Path) -> str | None:
    """SchemaStore URL whose fileMatch patterns match `path`."""
    catalog = net.get_json(CATALOG_URL, timeout=5)
    if not isinstance(catalog, dict):
        return None
    name = path.name
    full = path.expanduser().resolve().as_posix()
    for entry in catalog.get("schemas", []):
        for pattern in entry.get("fileMatch") or []:
            if pattern.startswith("!"):
                continue
            if "/" in pattern:
                hit = fnmatch(full, pattern) or fnmatch(full, "*/" + pattern)
            else:
                hit = fnmatch(name, pattern)
            if hit and entry.get("url"):
                return entry["url"]
    return None


def find(doc: Document, explicit: str | None = None) -> list[Schema]:
    """Schemas for `doc`, strongest last (later ones override earlier ones)."""
    found: list[Schema] = []
    seen: set[str] = set()

    def add(ref: str | None, prefix: str) -> None:
        if not ref:
            return
        url = to_url(ref, doc.path.parent)
        if url in seen:
            return
        seen.add(url)
        if schema := load(ref, prefix, doc.path.parent):
            found.insert(0, schema)

    add(explicit, "schema")
    add(declared(doc), "schema")
    add(catalog_match(doc.path), "schemastore")
    return found


# --- Resolution --------------------------------------------------------------


def _deref(ref: str, scope: Scope) -> tuple[dict[str, Any], Scope] | None:
    url, _, pointer = urljoin(scope.base, ref).partition("#")
    if url and url != scope.base.split("#", 1)[0]:
        root = fetch(url)
        if root is None:
            return None
        scope = Scope(root, url)
    node: Any = scope.root
    for part in pointer.lstrip("/").split("/") if pointer else []:
        part = unquote(part).replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return (node, scope) if isinstance(node, dict) else None


def resolve(sub: dict[str, Any], scope: Scope, _seen: frozenset[str] = frozenset()) -> tuple[dict[str, Any], Scope]:
    """Inline $ref, merge allOf, and unwrap "X or null" unions. Other
    branching keywords are left in place."""
    ref = sub.get("$ref")
    if isinstance(ref, str):
        key = urljoin(scope.base, ref)
        if key not in _seen and (target := _deref(ref, scope)):
            node, target_scope = target
            merged = {**node, **{k: v for k, v in sub.items() if k != "$ref"}}
            return resolve(merged, target_scope, _seen | {key})
    if isinstance(sub.get("allOf"), list):
        merged = {k: v for k, v in sub.items() if k != "allOf"}
        for entry in sub["allOf"]:
            if isinstance(entry, dict):
                part, _ = resolve(entry, scope, _seen)
                for k, v in part.items():
                    if k == "properties" and isinstance(v, dict):
                        merged["properties"] = {**merged.get("properties", {}), **v}
                    elif k == "required" and isinstance(v, list):
                        merged["required"] = [*merged.get("required", []), *v]
                    else:
                        merged.setdefault(k, v)
        sub = merged
    for keyword in ("anyOf", "oneOf"):
        branches = sub.get(keyword)
        if not isinstance(branches, list) or len(branches) < 2:
            continue
        real = [b for b in branches if not (isinstance(b, dict) and b.get("type") == "null")]
        if len(real) == 1 and isinstance(real[0], dict):
            part, part_scope = resolve(real[0], scope, _seen)
            return {**part, **{k: v for k, v in sub.items() if k != keyword}}, part_scope
    return sub, scope


def _branches(sub: dict[str, Any], scope: Scope) -> list[tuple[dict[str, Any], Scope]]:
    out = []
    for key in ("anyOf", "oneOf"):
        for branch in sub.get(key) or []:
            if isinstance(branch, dict):
                out.append(resolve(branch, scope))
    return out


def _child(node: dict[str, Any], scope: Scope, part: str) -> tuple[dict[str, Any], Scope] | None:
    """Subschema for key `part` inside the object/array schema `node`."""
    node, scope = resolve(node, scope)
    props = node.get("properties")
    if isinstance(props, dict) and isinstance(props.get(part), dict):
        return props[part], scope
    if part.isdigit():
        prefix = node.get("prefixItems")
        if isinstance(prefix, list) and int(part) < len(prefix) and isinstance(prefix[int(part)], dict):
            return prefix[int(part)], scope
        if isinstance(node.get("items"), dict):
            return node["items"], scope
    for pattern, sub in (node.get("patternProperties") or {}).items():
        try:
            if isinstance(sub, dict) and re.search(pattern, part):
                return sub, scope
        except re.error:
            continue
    for branch, branch_scope in _branches(node, scope):
        if hit := _child(branch, branch_scope, part):
            return hit
    if isinstance(node.get("additionalProperties"), dict):
        return node["additionalProperties"], scope
    return None


def walk(schema: Schema, path: KeyPath) -> tuple[dict[str, Any], Scope] | None:
    """Resolved subschema for `path`, or None when the schema doesn't cover it."""
    node, scope = schema.scope.root, schema.scope
    for part in path:
        hit = _child(node, scope, part)
        if hit is None:
            return None
        node, scope = hit
    return resolve(node, scope)


# --- Specs -------------------------------------------------------------------


def _first_type(sub: dict[str, Any], branches: list[tuple[dict[str, Any], Scope]]) -> str | None:
    ty = sub.get("type")
    if isinstance(ty, list):
        ty = next((t for t in ty if t != "null"), None)
    if ty is None:
        for branch, _ in branches:
            bt = branch.get("type")
            bt = next((t for t in bt if t != "null"), None) if isinstance(bt, list) else bt
            if bt and bt != "null":
                return bt
    return ty


def _choices(sub: dict[str, Any], branches: list[tuple[dict[str, Any], Scope]]) -> tuple[list[Any], list[str], bool]:
    """(options, notes, strict) from enum/const, including anyOf/oneOf branches."""
    if isinstance(sub.get("enum"), list):
        notes = sub.get("markdownEnumDescriptions") or sub.get("enumDescriptions") or []
        return list(sub["enum"]), [str(n) for n in notes], True
    if "const" in sub:
        return [sub["const"]], [], True
    options: list[Any] = []
    notes: list[str] = []
    open_branch = False
    for branch, _ in branches:
        if isinstance(branch.get("enum"), list):
            branch_notes = branch.get("markdownEnumDescriptions") or branch.get("enumDescriptions") or []
            for i, value in enumerate(branch["enum"]):
                if value not in options:
                    options.append(value)
                    notes.append(str(branch_notes[i]) if i < len(branch_notes) else "")
        elif "const" in branch:
            if branch["const"] not in options:
                options.append(branch["const"])
                notes.append(_text(branch))
        elif branch.get("type") != "null":
            open_branch = True
    if not any(notes):
        notes = []
    return options, notes, bool(options) and not open_branch


def _text(sub: dict[str, Any]) -> str:
    return str(sub.get("markdownDescription") or sub.get("description") or sub.get("title") or "").strip()


def spec_for(sub: dict[str, Any], scope: Scope) -> FieldSpec:
    """FieldSpec from a resolved subschema."""
    branches = _branches(sub, scope)
    spec = FieldSpec()
    spec.description = _text(sub) or next((_text(b) for b, _ in branches if _text(b)), "")
    if "default" in sub:
        spec.default = sub["default"]
    if sub.get("deprecated") is True or sub.get("deprecationMessage") or sub.get("markdownDeprecationMessage"):
        spec.deprecated = str(sub.get("deprecationMessage") or sub.get("markdownDeprecationMessage") or "Deprecated.")
    options, notes, strict = _choices(sub, branches)
    ty = _first_type(sub, branches)
    if options and ty != "array":
        spec.widget = "choice"
        spec.options, spec.option_notes, spec.strict = options, notes or None, strict
        if not strict:
            spec.warning = "Options from the schema; other values are also allowed."
    elif ty == "boolean":
        spec.widget = "switch"
    elif ty in ("integer", "number"):
        spec.widget = "number"
        spec.integer = ty == "integer"
    elif ty == "array":
        spec.widget = "list"
        items = sub.get("items")
        if isinstance(items, dict):
            items, item_scope = resolve(items, scope)
            item_type = _first_type(items, _branches(items, item_scope))
            if item_type in ("string", "integer", "number", "boolean"):
                spec.item_type = item_type
            elif item_type in ("object", "array"):
                spec.widget = "readonly"
            item_options, _, _ = _choices(items, _branches(items, item_scope))
            spec.item_options = item_options or None
    elif ty == "object":
        spec.widget = "readonly"
    elif ty == "string":
        pattern = str(sub.get("pattern", ""))
        if sub.get("format") == "color" or (("#" in pattern or "0x" in pattern) and "a-f" in pattern.lower()):
            spec.widget = "color"
        else:
            spec.widget = "text"
        examples = [e for e in sub.get("examples") or [] if isinstance(e, str)]
        if examples:
            spec.widget = "choice"
            spec.options = examples
            spec.warning = "Options are schema examples; other values are also allowed."
    for key, attr in (("minimum", "minimum"), ("exclusiveMinimum", "minimum"), ("maximum", "maximum"), ("exclusiveMaximum", "maximum")):
        if isinstance(sub.get(key), (int, float)) and not isinstance(sub.get(key), bool):
            setattr(spec, attr, sub[key])
    if spec.widget == "" and isinstance(spec.default, str) and COLOR_RE.match(spec.default):
        spec.widget = "color"
    return spec


# --- Listing declared keys ---------------------------------------------------


@dataclass
class Declared:
    leaves: dict[KeyPath, FieldSpec]
    sections: set[KeyPath]  # objects with named properties
    open_sections: set[KeyPath]  # objects that accept any key name
    skipped: int  # branching objects whose properties were not listed


def declared_keys(schema: Schema) -> Declared:
    """Every key path the schema declares outside branching keywords."""
    out = Declared({}, set(), set(), 0)

    def visit(node: dict[str, Any], scope: Scope, prefix: KeyPath, refs: frozenset[str]) -> None:
        if len(prefix) > 12:
            return
        if any(b.get("properties") for b, _ in _branches(node, scope)) or any(k in node for k in _CONDITIONAL[2:]):
            out.skipped += 1
        props = node.get("properties")
        addl = node.get("additionalProperties")
        if (
            isinstance(node.get("patternProperties"), dict)
            or isinstance(addl, dict)
            or addl is True
            or (addl is None and not props and node.get("type") == "object")
        ):
            out.open_sections.add(prefix)
        if not isinstance(props, dict):
            return
        for key, sub in props.items():
            if not isinstance(sub, dict):
                continue
            path = prefix + (key,)
            ref = urljoin(scope.base, sub["$ref"]) if isinstance(sub.get("$ref"), str) else ""
            if ref in refs:
                continue  # recursive definition
            resolved, sub_scope = resolve(sub, scope)
            addl = resolved.get("additionalProperties")
            is_object = (
                bool(resolved.get("properties"))
                or isinstance(resolved.get("patternProperties"), dict)
                or isinstance(addl, dict)
                or (resolved.get("type") == "object" and addl is not False)
            )
            if is_object:
                out.sections.add(path)
                visit(resolved, sub_scope, path, refs | {ref} if ref else refs)
            else:
                spec = spec_for(resolved, sub_scope)
                spec.source = schema.label
                out.leaves[path] = spec

    root, scope = resolve(schema.scope.root, schema.scope)
    visit(root, scope, (), frozenset())
    return out


# --- Validation --------------------------------------------------------------


def validate(data: Any, schema: Schema, limit: int = 50) -> list[str]:
    """Problems found in `data`, as "key.path: message" lines."""
    try:
        from jsonschema import validators
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT7

        def retrieve(uri: str) -> Resource:
            root = fetch(uri)
            if root is None:
                raise LookupError(uri)
            return Resource.from_contents(root, default_specification=DRAFT7)

        root = dict(schema.scope.root)
        root.setdefault("$id", schema.scope.base)
        cls = validators.validator_for(root, default=validators.Draft7Validator)
        validator = cls(root, registry=Registry(retrieve=retrieve))
        problems = []
        for error in validator.iter_errors(data):
            where = ".".join(str(p) for p in error.absolute_path) or "(top level)"
            message = error.message if len(error.message) <= 200 else error.message[:197] + "…"
            problems.append(f"{where}: {message}")
            if len(problems) >= limit:
                problems.append("…")
                break
        return problems
    except Exception as exc:  # broken or unreachable schema: report, don't crash
        return [f"schema check failed: {exc}"]
