"""Schema cascade: infer field metadata from multiple sources.

Sources, in priority order (later sources override earlier ones):
  1. Type inference from the current value (always works, weakest signal)
  2. `$schema` URL embedded in the file (strongest signal when present)
  3. SchemaStore.org catalog match by filename (covers most popular configs)

The output is a FieldSpec per leaf path, which the TUI uses to pick a widget.
"""
from __future__ import annotations

import fnmatch
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import requests

from ezconfig.formats import Document, Path_

CACHE_DIR = Path.home() / ".cache" / "ezconfig"
CATALOG_URL = "https://www.schemastore.org/api/json/catalog.json"
CATALOG_TTL_SEC = 24 * 60 * 60

HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


@dataclass
class FieldSpec:
    """How to render and validate a single field."""
    widget: str = "text"  # switch | color | number | text | select | multiselect | list
    description: str = ""
    default: Any = None
    enum: list[Any] | None = None
    minimum: float | None = None
    maximum: float | None = None
    is_integer: bool = False
    item_type: str = "string"  # for list widget: 'string' | 'integer' | 'number' | 'boolean'
    item_enum: list[Any] | None = None  # for list widget: enum constraints on items
    source: str = "inferred"  # inferred | embedded-schema | schemastore | heuristic-* | web (...)
    warning: str = ""  # only set for non-authoritative sources (heuristics, web scraping)

    def merge(self, other: "FieldSpec") -> "FieldSpec":
        """Return a new spec with `other`'s fields overriding ours when set."""
        return FieldSpec(
            widget=other.widget or self.widget,
            description=other.description or self.description,
            default=other.default if other.default is not None else self.default,
            enum=other.enum if other.enum is not None else self.enum,
            minimum=other.minimum if other.minimum is not None else self.minimum,
            maximum=other.maximum if other.maximum is not None else self.maximum,
            is_integer=other.is_integer or self.is_integer,
            item_type=other.item_type if other.item_type != "string" else self.item_type,
            item_enum=other.item_enum if other.item_enum is not None else self.item_enum,
            source=other.source if other.source != "inferred" else self.source,
            warning=other.warning or self.warning,
        )


@dataclass
class SchemaIndex:
    """All FieldSpecs for a document, keyed by path."""
    specs: dict[Path_, FieldSpec] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    # Schema-declared paths the doc doesn't have yet (ghosts).
    ghost_specs: dict[Path_, FieldSpec] = field(default_factory=dict)
    # Schema-declared sections (objects with properties) — used for tree.
    schema_sections: set[Path_] = field(default_factory=set)
    # Sections that accept arbitrary keys (patternProperties/additionalProperties).
    pattern_sections: set[Path_] = field(default_factory=set)
    # Paths marked deprecated.
    deprecated: set[Path_] = field(default_factory=set)
    # How many conditional schema branches were skipped during enumeration.
    skipped_conditionals: int = 0

    def get(self, path: Path_) -> FieldSpec:
        return self.specs.get(path) or self.ghost_specs.get(path, FieldSpec())


# --- 1. Type inference -------------------------------------------------------

def infer_spec(value: Any) -> FieldSpec:
    if isinstance(value, bool):
        return FieldSpec(widget="switch")
    if isinstance(value, int):
        return FieldSpec(widget="number", is_integer=True)
    if isinstance(value, float):
        return FieldSpec(widget="number")
    if isinstance(value, str):
        if HEX_COLOR_RE.match(value):
            return FieldSpec(widget="color")
        return FieldSpec(widget="text")
    if isinstance(value, list):
        item_type = _infer_item_type(value)
        return FieldSpec(widget="list", item_type=item_type)
    return FieldSpec(widget="text")


def _infer_item_type(items: list[Any]) -> str:
    if not items:
        return "string"
    sample = items[0]
    if isinstance(sample, bool):
        return "boolean"
    if isinstance(sample, int):
        return "integer"
    if isinstance(sample, float):
        return "number"
    return "string"


# --- 2 & 3. JSON Schema --------------------------------------------------------

def spec_from_subschema(sub: dict[str, Any], root: dict[str, Any] | None = None) -> FieldSpec:
    """Translate a JSON Schema fragment for a single field into a FieldSpec."""
    sub, root = _unwrap_schema(sub, root)
    spec = FieldSpec()
    spec.description = sub.get("description", "") or sub.get("title", "")
    if "default" in sub:
        spec.default = sub["default"]
    if "enum" in sub:
        spec.enum = list(sub["enum"])
        spec.widget = "select"
    ty = sub.get("type")
    if isinstance(ty, list):
        ty = next((t for t in ty if t != "null"), ty[0])
    if not spec.enum:
        if ty == "boolean":
            spec.widget = "switch"
        elif ty == "integer":
            spec.widget = "number"
            spec.is_integer = True
        elif ty == "number":
            spec.widget = "number"
        elif ty == "array":
            spec.widget = "list"
            items_sub = sub.get("items")
            if isinstance(items_sub, dict):
                items_resolved, _ = _unwrap_schema(items_sub, root)
                items_ty = items_resolved.get("type")
                if isinstance(items_ty, list):
                    items_ty = next((t for t in items_ty if t != "null"), items_ty[0])
                if items_ty in ("string", "integer", "number", "boolean"):
                    spec.item_type = items_ty
                if "enum" in items_resolved:
                    spec.item_enum = list(items_resolved["enum"])
        elif ty == "string":
            fmt = sub.get("format", "")
            if fmt == "color" or sub.get("x-widget") == "color":
                spec.widget = "color"
            else:
                spec.widget = "text"
    if "minimum" in sub:
        spec.minimum = float(sub["minimum"])
    if "maximum" in sub:
        spec.maximum = float(sub["maximum"])
    return spec


def _resolve_pointer(node: Any, fragment: str) -> dict[str, Any] | None:
    """Walk a JSON Pointer fragment (without leading #) into `node`."""
    if not fragment:
        return node if isinstance(node, dict) else None
    for part in fragment.lstrip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node if isinstance(node, dict) else None


def _resolve_ref(ref: str, root: dict[str, Any] | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve a $ref. Returns (target, new_root).

    For local refs (#/...) new_root is the same root.
    For remote refs (http://, https://) the schema is fetched and becomes the
    new root for any further local refs inside the returned target.
    """
    if not isinstance(ref, str):
        return None, root
    if ref.startswith("#"):
        if root is None:
            return None, root
        return _resolve_pointer(root, ref[1:]), root
    if ref.startswith(("http://", "https://")):
        base, _, frag = ref.partition("#")
        fetched = fetch_schema(base)
        if fetched is None:
            return None, root
        target = _resolve_pointer(fetched, frag) if frag else fetched
        return target, fetched
    return None, root


def _unwrap_schema(
    sub: dict[str, Any],
    root: dict[str, Any] | None = None,
    _seen: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Inline $ref, merge allOf, pick anyOf/oneOf head. Returns (resolved, current_root).

    `current_root` may differ from the passed `root` if a remote $ref was followed —
    callers should use the returned root when resolving subsequent refs in the result.
    """
    _seen = set() if _seen is None else _seen
    ref = sub.get("$ref")
    if isinstance(ref, str) and ref not in _seen:
        _seen = _seen | {ref}
        target, new_root = _resolve_ref(ref, root)
        if target is not None:
            merged = {**target, **{k: v for k, v in sub.items() if k != "$ref"}}
            return _unwrap_schema(merged, new_root, _seen)
    if "allOf" in sub and isinstance(sub["allOf"], list):
        merged: dict[str, Any] = {k: v for k, v in sub.items() if k != "allOf"}
        for entry in sub["allOf"]:
            if not isinstance(entry, dict):
                continue
            resolved, _ = _unwrap_schema(entry, root, _seen)
            for k, v in resolved.items():
                if k == "properties" and isinstance(v, dict):
                    existing = merged.get("properties", {})
                    merged["properties"] = {**existing, **v}
                elif k == "required" and isinstance(v, list):
                    merged.setdefault("required", [])
                    merged["required"].extend(v)
                else:
                    merged.setdefault(k, v)
        return merged, root
    for key in ("anyOf", "oneOf"):
        if key in sub and isinstance(sub[key], list) and sub[key]:
            head = sub[key][0]
            if isinstance(head, dict):
                resolved, head_root = _unwrap_schema(head, root, _seen)
                merged = {k: v for k, v in sub.items() if k not in ("anyOf", "oneOf")}
                for k, v in resolved.items():
                    merged.setdefault(k, v)
                return merged, head_root
    return sub, root


def walk_schema(schema: dict[str, Any], path: Path_) -> dict[str, Any] | None:
    """Navigate a JSON Schema to the subschema for `path`. Returns None on miss."""
    node, root = _unwrap_schema(schema, schema)
    for part in path:
        node, root = _unwrap_schema(node, root)
        props = node.get("properties")
        if isinstance(props, dict) and part in props:
            node = props[part]
            continue
        pat = node.get("patternProperties")
        if isinstance(pat, dict):
            matched = next(
                (v for p, v in pat.items() if re.search(p, part)), None
            )
            if matched is not None:
                node = matched
                continue
        addl = node.get("additionalProperties")
        if isinstance(addl, dict):
            node = addl
            continue
        return None
    if not isinstance(node, dict):
        return None
    resolved, _ = _unwrap_schema(node, root)
    return resolved


# --- Strict unwrap & enumeration --------------------------------------------

# Keys we explicitly refuse to descend into when enumerating unset paths.
# Each one represents a *conditional* schema branch where the set of legal
# properties depends on other values — surfacing those keys would be false
# discoverability. We count them so the UI can tell the user how many were
# skipped.
_CONDITIONAL_KEYS = ("oneOf", "anyOf", "if", "then", "else", "dependentSchemas")


def _strict_unwrap(
    sub: dict[str, Any],
    root: dict[str, Any] | None,
    skipped_counter: list[int],
    _seen: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Like _unwrap_schema, but never descends conditional branches.

    Only inlines $ref and merges allOf — the two "always-applies" combinators.
    Conditional keys (oneOf/anyOf/if/then/else/dependentSchemas) are counted
    via `skipped_counter[0] += 1` and not followed.
    """
    _seen = set() if _seen is None else _seen
    ref = sub.get("$ref")
    if isinstance(ref, str) and ref not in _seen:
        _seen = _seen | {ref}
        target, new_root = _resolve_ref(ref, root)
        if target is not None:
            merged = {**target, **{k: v for k, v in sub.items() if k != "$ref"}}
            return _strict_unwrap(merged, new_root, skipped_counter, _seen)
    if "allOf" in sub and isinstance(sub["allOf"], list):
        merged: dict[str, Any] = {k: v for k, v in sub.items() if k != "allOf"}
        for entry in sub["allOf"]:
            if not isinstance(entry, dict):
                continue
            resolved, _ = _strict_unwrap(entry, root, skipped_counter, _seen)
            for k, v in resolved.items():
                if k == "properties" and isinstance(v, dict):
                    existing = merged.get("properties", {})
                    merged["properties"] = {**existing, **v}
                elif k == "required" and isinstance(v, list):
                    merged.setdefault("required", [])
                    merged["required"].extend(v)
                else:
                    merged.setdefault(k, v)
        return merged, root
    # Count any conditional combinators present, then ignore them.
    for cond in _CONDITIONAL_KEYS:
        if cond in sub:
            skipped_counter[0] += 1
    return sub, root


def enumerate_schema(
    schema: dict[str, Any],
) -> tuple[dict[Path_, FieldSpec], set[Path_], set[Path_], set[Path_], int]:
    """Walk every enumerable property path in `schema`.

    Returns (leaves, sections, pattern_sections, deprecated, skipped_count).
    - leaves: FieldSpec per leaf path the schema declares
    - sections: paths that resolve to an object with properties
    - pattern_sections: paths whose schema permits arbitrary keys
    - deprecated: paths whose schema sets `deprecated: true`
    - skipped_count: number of conditional branches not followed
    """
    leaves: dict[Path_, FieldSpec] = {}
    sections: set[Path_] = set()
    pattern_sections: set[Path_] = set()
    deprecated: set[Path_] = set()
    skipped = [0]
    seen_node_ids: set[int] = set()  # cycle detection on resolved dicts

    def visit(node: dict[str, Any], root: dict[str, Any] | None, prefix: Path_) -> None:
        if id(node) in seen_node_ids:
            return
        resolved, sub_root = _strict_unwrap(node, root, skipped)
        if id(resolved) in seen_node_ids:
            return
        seen_node_ids.add(id(resolved))

        # Pattern / additional properties → mark this prefix as accepting custom keys.
        if isinstance(resolved.get("patternProperties"), dict):
            pattern_sections.add(prefix)
        addl = resolved.get("additionalProperties")
        if isinstance(addl, dict) or addl is True:
            pattern_sections.add(prefix)

        props = resolved.get("properties")
        if not isinstance(props, dict):
            return

        for key, sub in props.items():
            if not isinstance(sub, dict):
                continue
            sub_path = prefix + (key,)
            sub_resolved, sub_sub_root = _strict_unwrap(sub, sub_root, skipped)
            if sub_resolved.get("deprecated") is True:
                deprecated.add(sub_path)
            inner_props = sub_resolved.get("properties")
            inner_pattern = sub_resolved.get("patternProperties")
            inner_addl = sub_resolved.get("additionalProperties")
            has_pattern = isinstance(inner_pattern, dict) or isinstance(inner_addl, dict) or inner_addl is True
            if (isinstance(inner_props, dict) and inner_props) or has_pattern:
                # Object-shaped node: either has named sub-properties, or accepts
                # arbitrary keys, or both. Recurse so visit() marks pattern_sections
                # and enumerates any named children.
                sections.add(sub_path)
                visit(sub, sub_root, sub_path)
            else:
                # True leaf — primitive value or array.
                spec = spec_from_subschema(sub_resolved, sub_sub_root)
                spec.source = "schema (unset)"
                leaves[sub_path] = spec

    visit(schema, schema, ())
    return leaves, sections, pattern_sections, deprecated, skipped[0]


# --- Embedded $schema --------------------------------------------------------

def find_embedded_schema_url(doc: Document) -> str | None:
    """Look for `$schema` at the document root."""
    data = doc.data
    if isinstance(data, dict):
        val = data.get("$schema")
        if isinstance(val, str):
            return val
    return None


# --- SchemaStore lookup ------------------------------------------------------

def load_schemastore_catalog() -> list[dict[str, Any]]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / "catalog.json"
    if cache_file.exists() and time.time() - cache_file.stat().st_mtime < CATALOG_TTL_SEC:
        try:
            return json.loads(cache_file.read_text()).get("schemas", [])
        except (json.JSONDecodeError, OSError):
            pass
    try:
        resp = requests.get(CATALOG_URL, timeout=5)
        resp.raise_for_status()
        cache_file.write_text(resp.text)
        return resp.json().get("schemas", [])
    except (requests.RequestException, json.JSONDecodeError, OSError):
        return []


def match_catalog(filename: str, catalog: Iterable[dict[str, Any]]) -> str | None:
    for entry in catalog:
        for pattern in entry.get("fileMatch", []) or []:
            if fnmatch.fnmatch(filename, pattern):
                return entry.get("url")
    return None


def fetch_schema(url: str) -> dict[str, Any] | None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = re.sub(r"[^A-Za-z0-9._-]", "_", url)
    cache_file = CACHE_DIR / f"schema_{key}.json"
    if cache_file.exists() and time.time() - cache_file.stat().st_mtime < CATALOG_TTL_SEC:
        try:
            return json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        cache_file.write_text(resp.text)
        return resp.json()
    except (requests.RequestException, json.JSONDecodeError, OSError):
        return None


# --- Top-level cascade -------------------------------------------------------

def build_index(
    doc: Document,
    web_url: str | None = None,
    web_urls: list[str] | None = None,
) -> SchemaIndex:
    """Run the full cascade for `doc` and return per-path FieldSpecs.

    When `web_urls` (or the single-URL `web_url`) is provided, an extra stage
    scrapes those pages for value mentions next to each text-typed key. When
    multiple URLs are given, every page is queried per-key and the most
    informative hit wins (the source label records which page it came from).
    Discovered options override `inferred` and `heuristic` sources but never
    a schema-driven enum.
    """
    index = SchemaIndex()
    schemas_used: list[tuple[dict[str, Any], str]] = []

    embedded_url = find_embedded_schema_url(doc)
    if embedded_url:
        s = fetch_schema(embedded_url)
        if s:
            schemas_used.append((s, f"embedded ({embedded_url})"))

    catalog = load_schemastore_catalog()
    if catalog:
        url = match_catalog(doc.source_path.name, catalog)
        if url and url != embedded_url:
            s = fetch_schema(url)
            if s:
                schemas_used.append((s, f"schemastore ({url})"))

    # Local imports to avoid circular deps. Both modules import lazily from us
    # at definition time; lazy-importing here keeps the graph one-directional.
    from ezconfig.heuristics import lookup as heuristic_lookup
    from ezconfig.web_options import fetch_page as fetch_web_page

    # Build the list of pages to query. Both web_url (single) and web_urls
    # (list) are accepted; duplicates are folded out, order preserved.
    requested_urls: list[str] = []
    for u in (web_urls or []):
        if u and u not in requested_urls:
            requested_urls.append(u)
    if web_url and web_url not in requested_urls:
        requested_urls.append(web_url)
    # (url, WebPage|None). None means the fetch failed; kept so the sources
    # list can show which page didn't load.
    fetched_pages = [(u, fetch_web_page(u)) for u in requested_urls]
    live_pages = [(u, p) for u, p in fetched_pages if p is not None]
    web_fired = False

    heuristics_fired = False
    for path, value in doc.walk():
        spec = infer_spec(value)
        for schema, label in schemas_used:
            sub = walk_schema(schema, path)
            if sub:
                schema_spec = spec_from_subschema(sub, schema)
                schema_spec.source = label
                spec = spec.merge(schema_spec)
        # Heuristics only fire when nothing authoritative already set an enum.
        # Schema-driven enums always win; we never override them with a guess.
        if not spec.enum and path:
            hit = heuristic_lookup(path[-1], value)
            if hit is not None:
                spec.enum = hit.options
                spec.widget = "select"
                spec.warning = hit.warning
                spec.source = hit.source
                heuristics_fired = True
        # Web options: overrides inferred + heuristic, but never schema.
        # Only applies to string-valued text fields — switches, numbers, colors
        # and lists already have stronger widgets.
        if (
            live_pages
            and path
            and spec.widget in ("text", "select")
            and (spec.source == "inferred" or spec.source.startswith("heuristic"))
        ):
            # Query every page and keep the most-informative hit (most values).
            # Picking per-key (vs. unioning across pages) keeps the dropdown
            # internally consistent — pages tend to describe disjoint key
            # domains (e.g. ghostty vs. micro) so unioning would mix worlds.
            best_url: str | None = None
            best_hit = None
            for url, page in live_pages:
                hit = page.lookup(path[-1])
                if hit is None or not hit.values:
                    continue
                if best_hit is None or len(hit.values) > len(best_hit.values):
                    best_url = url
                    best_hit = hit
            if best_hit is not None and best_url is not None:
                options = list(best_hit.values)
                # Keep the user's current value selectable even if the docs
                # didn't list it — same pattern as the heuristics layer.
                if isinstance(value, str) and value and value not in options:
                    options = [value] + options
                # Build a FieldSpec for the web hit and merge — same shape as
                # the schema stage above. The excerpt lives in `description`
                # so it renders through the same row schemas use.
                web_spec = FieldSpec(
                    widget="select",
                    enum=options,
                    description=best_hit.excerpt,
                    source=f"web ({best_url})",
                    warning="From web docs — may be incomplete or use different terminology.",
                )
                spec = spec.merge(web_spec)
                web_fired = True
        # Adapter-supplied warnings (currently only TextDocument tags hex/bool/
        # number/string tokens with kind-specific risk notes).
        doc_warnings = getattr(doc, "path_warnings", {})
        if path in doc_warnings and not spec.warning:
            spec.warning = doc_warnings[path]
        index.specs[path] = spec

    # Schema enumeration: find paths the schema declares but the doc doesn't have.
    if schemas_used:
        all_leaves: dict[Path_, FieldSpec] = {}
        all_sections: set[Path_] = set()
        all_patterns: set[Path_] = set()
        all_deprecated: set[Path_] = set()
        total_skipped = 0
        for schema, _label in schemas_used:
            leaves, sections, patterns, deprec, skipped = enumerate_schema(schema)
            for p, s in leaves.items():
                # Later schemas override earlier ones on the same path.
                all_leaves[p] = s
            all_sections |= sections
            all_patterns |= patterns
            all_deprecated |= deprec
            total_skipped += skipped
        # Ghost = schema-declared leaf NOT already present in the doc.
        for p, s in all_leaves.items():
            if p not in index.specs:
                index.ghost_specs[p] = s
        index.schema_sections = all_sections
        index.pattern_sections = all_patterns
        index.deprecated = all_deprecated
        index.skipped_conditionals = total_skipped

    # Web-discovered ghost keys: scrape every live page for keys the docs
    # mention but the user's file doesn't have, and surface them via the same
    # ghost_specs / show_unset machinery that schemas use. Skipped for the
    # text-fallback format because its adapter can't serialize new keys back
    # to disk — ghost promotion would silently lose the change on save.
    web_ghosts_added = 0
    if live_pages and doc.format_name != "text":
        existing_top: set[str] = {p[0] for p in index.specs if p}
        existing_top |= {p[0] for p in index.ghost_specs if p}
        # Per-key, pick the page that had the most candidate values for that key.
        best: dict[str, tuple[str, Any]] = {}
        for url, page in live_pages:
            for key, hit in page.discover_keys().items():
                if key in existing_top:
                    continue
                prev = best.get(key)
                if prev is None or len(hit.values) > len(prev[1].values):
                    best[key] = (url, hit)
        for key, (url, hit) in best.items():
            # Shaped identically to a schema-discovered ghost — same fields,
            # same widget rules — with the docs excerpt in `description` (where
            # a schema would put its own description) and a short warning
            # tagging the source as non-authoritative.
            ghost = FieldSpec(
                widget="select" if hit.values else "text",
                enum=list(hit.values) if hit.values else None,
                description=hit.excerpt,
                source=f"web ({url})",
                warning="From web docs — may be incomplete or use different terminology.",
            )
            index.ghost_specs[(key,)] = ghost
            web_ghosts_added += 1

    index.sources = [label for _, label in schemas_used] or ["inferred from values"]
    if heuristics_fired:
        index.sources.append("+ key-name heuristics")
    for url, page in fetched_pages:
        if page is None:
            index.sources.append(f"+ web ({url}) — fetch failed")
        else:
            index.sources.append(f"+ web ({url})")
    if requested_urls and not web_fired and not web_ghosts_added:
        index.sources.append("(web pages loaded but no key matched)")
    if web_ghosts_added:
        index.sources.append(f"+ {web_ghosts_added} key(s) discovered from docs")
    return index
