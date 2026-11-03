"""Build a FieldSpec for every key: from the value, schemas, docs, and guesses.

Precedence for options: schema, then docs, then guesses. Keys that a schema or
the docs describe but the file doesn't have are "unset keys".
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Sequence

from ezconfig import docs as docs_mod
from ezconfig import heuristics, schema as schema_mod
from ezconfig.document import Document, KeyPath, parse_literal
from ezconfig.schema import Schema
from ezconfig.spec import FieldSpec, infer

DOCS_WARNING = "From docs; options may be incomplete."


@dataclass
class Index:
    specs: dict[KeyPath, FieldSpec] = field(default_factory=dict)  # keys in the file
    unset: dict[KeyPath, FieldSpec] = field(default_factory=dict)  # known keys not in the file
    sections: set[KeyPath] = field(default_factory=set)  # sections a schema declares
    open_sections: set[KeyPath] = field(default_factory=set)  # sections that accept any key
    skipped: int = 0  # branching schema objects whose keys were not listed
    schemas: list[Schema] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)  # one line per schema/docs source

    def get(self, path: KeyPath) -> FieldSpec:
        return self.specs.get(path) or self.unset.get(path) or FieldSpec(widget="text")

    def mark_set(self, path: KeyPath) -> None:
        if path in self.unset:
            self.specs[path] = self.unset.pop(path)

    def mark_unset(self, path: KeyPath) -> None:
        spec = self.specs.pop(path, None)
        if spec is not None and spec.source != "value":
            self.unset[path] = spec


def _with_current(options: list[Any], value: Any) -> list[Any]:
    """Keep the current value selectable when the source didn't list it."""
    if isinstance(value, str) and value and value not in options:
        return [value, *options]
    return list(options)


def _best_hit(pages: list[docs_mod.DocsPage], name: str) -> tuple[docs_mod.DocsPage, docs_mod.DocsHit] | None:
    best = None
    for page in pages:
        hit = page.lookup(name)
        if hit and (best is None or len(hit.options) > len(best[1].options)):
            best = (page, hit)
    return best


def _guess(spec: FieldSpec, name: str, value: Any, doc: Document) -> FieldSpec:
    if spec.options:
        # Theme files next to the config are valid even when a schema lists built-ins.
        themes = [t for t in heuristics.theme_options(name, doc.path) if t not in spec.options]
        if themes:
            spec.options = [*spec.options, *themes]
        return spec
    if spec.widget not in ("text", ""):
        return spec
    g = heuristics.guess(name, value, doc.path)
    if g is None:
        return spec
    return spec.merge(FieldSpec(widget="choice", options=g.options, source=g.source, warning=g.warning))


_BOOL_WORDS = {"true", "false", "yes", "no", "on", "off"}


def _docs_options(hit: docs_mod.DocsHit, is_bool: bool, value: Any) -> list[Any]:
    """Options from a docs hit. For booleans the docs usually list only the
    extra words (e.g. "left", "right" besides true/false)."""
    if is_bool:
        extra = [o for o in hit.options if o.lower() not in _BOOL_WORDS]
        return [True, False, *extra] if extra else []
    return _with_current(hit.options, value)


def _describe(hit: docs_mod.DocsHit, name: str) -> str:
    """Docs excerpt without a first line that only repeats the key name."""
    first, _, rest = hit.excerpt.partition("\n")
    return rest.strip() if first.strip().strip("`").lower() == name.lower() and rest.strip() else hit.excerpt


def _from_docs(spec: FieldSpec, hit: docs_mod.DocsHit, name: str, value: Any) -> FieldSpec:
    has_schema = spec.source.startswith("schema")
    if hit.options and spec.widget in ("text", "choice", "switch", "") and not spec.strict and not has_schema:
        options = _docs_options(hit, spec.widget == "switch", value)
        if options:
            return spec.merge(FieldSpec(
                widget="choice",
                options=options,
                description=spec.description or _describe(hit, name),
                source=f"docs ({hit.source})",
                warning=DOCS_WARNING,
            ))
    if not spec.description:
        spec.description = _describe(hit, name)
    return spec


def _unset_from_docs(hit: docs_mod.DocsHit, name: str) -> FieldSpec:
    default = parse_literal(hit.default) if hit.default else None
    options = _docs_options(hit, isinstance(default, bool), None) if hit.options else []
    widget = "choice" if options else (infer(default).widget if default is not None else "text")
    return FieldSpec(
        widget=widget,
        options=options or None,
        description=_describe(hit, name),
        default=default,
        source=f"docs ({hit.source})",
        warning=DOCS_WARNING,
    )


def load_docs(sources: Sequence[str]) -> list[tuple[str, docs_mod.DocsPage | None]]:
    if not sources:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(sources))) as pool:
        return list(zip(sources, pool.map(docs_mod.load, sources)))


def build(doc: Document, docs: Sequence[str] = (), schema: str | None = None) -> Index:
    index = Index(schemas=schema_mod.find(doc, schema))
    loaded = load_docs(docs)
    pages = [page for _, page in loaded if page is not None]
    matched: dict[str, int] = {}  # page source -> keys in the file the docs describe
    found: dict[str, int] = {}  # page source -> keys added as unset keys

    for path, value in doc.walk():
        if not path:
            continue
        spec = infer(value)
        for s in index.schemas:
            if hit := schema_mod.walk(s, path):
                sub = schema_mod.spec_for(*hit)
                sub.source = s.label
                spec = spec.merge(sub)
        name = doc.name_of(path)
        if pages and (best := _best_hit(pages, name)):
            page, hit = best
            spec = _from_docs(spec, hit, name, value)
            matched[page.source] = matched.get(page.source, 0) + 1
        spec = _guess(spec, name, value, doc)
        if not spec.warning:
            spec.warning = doc.warning_for(path)
        spec.widget = spec.widget or "text"
        if spec.options:
            spec.options = _with_current(spec.options, value)
        index.specs[path] = spec

    for s in index.schemas:
        declared = schema_mod.declared_keys(s)
        for path, spec in declared.leaves.items():
            if not doc.has(path):
                index.unset[path] = spec
        index.sections |= declared.sections
        index.open_sections |= declared.open_sections
        index.skipped += declared.skipped

    if pages and doc.can_add and doc.format_name != "ini":
        _add_docs_keys(index, doc, pages, found)

    for path, spec in index.unset.items():
        if not spec.widget:
            spec.widget = infer(spec.default).widget if spec.default is not None else "text"
        index.unset[path] = _guess(spec, path[-1], spec.default, doc)

    index.sources = [s.label for s in index.schemas] or ["no schema"]
    for source, page in loaded:
        if page is None:
            index.sources.append(f"docs ({source}): failed to load")
        else:
            n, m = matched.get(page.source, 0), found.get(page.source, 0)
            index.sources.append(f"docs ({source}): {n} keys matched, {m} unset keys found")
    return index


def _add_docs_keys(index: Index, doc: Document, pages: list[docs_mod.DocsPage], found: dict[str, int]) -> None:
    names = {doc.name_of(p) for p in index.specs}
    data = doc.data
    flat_dotted = isinstance(data, dict) and any("." in str(k) for k in data)
    split = doc.format_name in ("toml", "yaml", "json") and not flat_dotted
    best: dict[KeyPath, tuple[docs_mod.DocsPage, docs_mod.DocsHit]] = {}
    for page in pages:
        for key, hit in page.discover_keys().items():
            path = tuple(key.split(".")) if split else (key,)
            if key in names or path in index.specs or path in index.unset or doc.has(path):
                continue
            if path not in best or len(hit.options) > len(best[path][1].options):
                best[path] = (page, hit)
    for path, (page, hit) in best.items():
        index.unset[path] = _unset_from_docs(hit, path[-1])
        found[page.source] = found.get(page.source, 0) + 1
