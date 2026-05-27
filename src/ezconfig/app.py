"""The Textual app: tree on the left, dynamic editor on the right."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Footer, Header, Input, Label, Select, Static, Switch, Tree
from textual.widgets.tree import TreeNode

from ezconfig.formats import Document, Path_, load as load_doc
from ezconfig.schema import FieldSpec, SchemaIndex, build_index
from ezconfig.widgets import HexColorPicker, ListEditor


class EzConfigApp(App):
    CSS = """
    Screen { layout: vertical; }
    #banner { height: auto; padding: 0 1; background: $warning; color: black; text-style: bold; display: none; }
    #banner.visible { display: block; }
    #main { height: 1fr; }
    #tree-pane { width: 40%; border-right: solid $accent; }
    #editor-pane { width: 1fr; padding: 1 2; }
    #status { dock: bottom; height: 1; padding: 0 1; background: $boost; color: $text; }
    .field-label { text-style: bold; padding-top: 1; }
    .field-desc { color: $text-muted; padding-bottom: 1; }
    .field-meta { color: $text-muted; padding-top: 1; }
    .field-warning { color: $warning; text-style: italic; padding-top: 1; }
    .placeholder { color: $text-muted; padding: 2; }
    Tree { padding: 1; }
    """

    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("ctrl+r", "reload", "Reload"),
        Binding("ctrl+u", "toggle_unset", "Show/hide unset"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, source_path: Path, web_urls: list[str] | None = None):
        super().__init__()
        self.source_path = source_path
        self.web_urls = list(web_urls) if web_urls else []
        self.doc: Document | None = None
        self.index: SchemaIndex | None = None
        self.current_path: Path_ | None = None
        self.current_is_ghost = False
        self.current_default: Any = None
        self._pattern_section_path: Path_ | None = None
        self.dirty = False
        self.show_unset = False

    # --- Layout --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="banner")
        with Horizontal(id="main"):
            yield Tree("config", id="tree-pane")
            yield VerticalScroll(id="editor-pane")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"ezconfig — {self.source_path.name}"
        self._reload()

    # --- Loading -------------------------------------------------------------

    def _reload(self) -> None:
        try:
            self.doc = load_doc(self.source_path)
            self.index = build_index(self.doc, web_urls=self.web_urls)
        except Exception as exc:
            self._set_status(f"load error: {exc}", error=True)
            return

        tree = self.query_one(Tree)
        tree.clear()
        tree.root.label = self.source_path.name
        self._populate(tree.root, self.doc.data, ())
        tree.root.expand()
        self.dirty = False
        self._update_banner()
        sources = ", ".join(self.index.sources)
        ghost_count = len(self.index.ghost_specs)
        hint = f"  ·  {ghost_count} unset key(s) — press Ctrl+U to view" if ghost_count else ""
        self._set_status(f"loaded {self.source_path} ({self.doc.format_name}) — schema: {sources}{hint}")

    def _update_banner(self) -> None:
        """Show a warning banner when the file was loaded via the text fallback."""
        banner = self.query_one("#banner", Static)
        if self.doc is not None and self.doc.format_name == "text":
            tokens = getattr(self.doc, "tokens", [])
            from collections import Counter
            kinds = Counter(t["kind"] for t in tokens)
            kind_summary = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
            banner.update(
                f"⚠ Text fallback — {len(tokens)} token(s) by regex ({kind_summary or 'none'}). "
                f"Detected values are editable; everything else is preserved as-is."
            )
            banner.add_class("visible")
        else:
            banner.update("")
            banner.remove_class("visible")

    def _populate(self, node: TreeNode, data: Any, prefix: Path_) -> None:
        """Populate one level of the tree at `prefix`. Lazy-expands deeper levels.

        Tree nodes carry a tuple in `.data`:
          ("section", path)       — real section that has children
          ("leaf",    path)       — real leaf (value in doc)
          ("ghost-section", path) — section the schema declares but doc doesn't
          ("ghost-leaf",    path) — schema-declared leaf not in doc
          ("pattern-add",   path) — '+ Add custom key' action on a pattern section
        """
        if not isinstance(data, dict):
            return
        idx = self.index
        children_at_this_level: set[str] = set()
        # Real keys first.
        for key, value in data.items():
            key_s = str(key)
            children_at_this_level.add(key_s)
            child_path = prefix + (key_s,)
            label = self._section_label(key_s, child_path) if isinstance(value, dict) and value else self._leaf_label(key_s, value, ghost=False)
            if isinstance(value, dict) and value:
                child = node.add(label, data=("section", child_path), expand=False)
                # Lazy: only fill section's children when expanded.
                child.add_leaf("…", data=("placeholder", child_path))
            else:
                node.add_leaf(label, data=("leaf", child_path))
        # Ghost keys (only when show_unset and we have schema data).
        if self.show_unset and idx is not None:
            ghost_keys = self._ghost_keys_at(prefix)
            for key_s in sorted(ghost_keys):
                if key_s in children_at_this_level:
                    continue
                child_path = prefix + (key_s,)
                if child_path in idx.schema_sections:
                    label = self._section_label(key_s, child_path, ghost=True)
                    child = node.add(label, data=("ghost-section", child_path), expand=False)
                    child.add_leaf("…", data=("placeholder", child_path))
                else:
                    spec = idx.ghost_specs.get(child_path, FieldSpec())
                    label = self._leaf_label(key_s, spec.default, ghost=True,
                                              deprecated=child_path in idx.deprecated)
                    node.add_leaf(label, data=("ghost-leaf", child_path))
        # Pattern-section "+ Add custom key" action.
        if idx is not None and prefix in idx.pattern_sections:
            node.add_leaf("+ Add custom key", data=("pattern-add", prefix))
        # Always offer a manual-add escape hatch at the root. Lets the user
        # type any key (dotted for nested) + value, regardless of whether
        # docs/schema discovered it. Useful when the web scraper missed an
        # option the user knows exists.
        if prefix == ():
            node.add_leaf("+ Add custom key (manual)", data=("manual-add", ()))

    def _ghost_keys_at(self, prefix: Path_) -> set[str]:
        """Top-level schema-declared key names that live directly under `prefix`."""
        if self.index is None:
            return set()
        out: set[str] = set()
        depth = len(prefix)
        for path in list(self.index.ghost_specs) + list(self.index.schema_sections):
            if len(path) > depth and path[:depth] == prefix:
                out.add(path[depth])
        return out

    def _expand_lazy(self, node: TreeNode) -> None:
        """If `node`'s children are still the placeholder, populate for real."""
        if not node.children:
            return
        first = node.children[0]
        if not first.data or first.data[0] != "placeholder":
            return
        node.remove_children()
        if not self.doc or not node.data:
            return
        _, path = node.data
        # Walk doc to the subdict at this path (may not exist for ghost sections).
        sub: Any = self.doc.data
        for part in path:
            if isinstance(sub, dict) and part in sub:
                sub = sub[part]
            else:
                sub = {}
                break
        self._populate(node, sub, path)

    def _section_label(self, key: str, path: Path_, ghost: bool = False) -> str:
        idx = self.index
        if idx is None or not self.show_unset:
            return f"{key}/" + (" (unset)" if ghost else "")
        set_count, unset_count = self._counts_under(path)
        if ghost:
            return f"{key}/  (unset — {unset_count} options)"
        if unset_count:
            return f"{key}/  ({set_count} set · {unset_count} unset)"
        return f"{key}/"

    def _counts_under(self, prefix: Path_) -> tuple[int, int]:
        """Count real-leaf vs ghost-leaf paths under `prefix` (any depth)."""
        idx = self.index
        if idx is None:
            return (0, 0)
        d = len(prefix)
        set_n = sum(1 for p in idx.specs if len(p) > d and p[:d] == prefix)
        unset_n = sum(1 for p in idx.ghost_specs if len(p) > d and p[:d] == prefix)
        return set_n, unset_n

    def _leaf_label(self, key: str, value: Any, ghost: bool = False, deprecated: bool = False) -> str:
        rendered = self._render_value(value)
        tag = ""
        if deprecated:
            tag += "  [DEPRECATED]"
        if ghost:
            return f"{key}: {rendered}  (unset){tag}"
        return f"{key}: {rendered}{tag}"

    def _render_value(self, value: Any) -> str:
        if isinstance(value, bool):
            return "on" if value else "off"
        if isinstance(value, list):
            return f"[{len(value)} items]"
        if value is None:
            return "(no default)"
        s = str(value)
        return s if len(s) <= 40 else s[:37] + "…"

    # --- Editor pane ---------------------------------------------------------

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        """Resolve lazy placeholders the moment a section is expanded."""
        self._expand_lazy(event.node)

    async def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data
        if not data:
            return
        kind, path = data
        editor = self.query_one("#editor-pane", VerticalScroll)
        # Await removal so the new editor-widget id doesn't collide with the
        # outgoing one (re-selecting the same node otherwise raises DuplicateIds).
        await editor.remove_children()
        if kind == "pattern-add":
            self.current_path = None
            await editor.mount(Static(f"Pattern section: {'.'.join(path) or '(root)'}", classes="field-label"))
            await editor.mount(Static(
                "This section accepts arbitrary keys. Type a name and value, then press Apply.",
                classes="field-desc",
            ))
            await editor.mount(Input(placeholder="key name", id="pattern-key"))
            await editor.mount(Input(placeholder="value", id="pattern-value"))
            await editor.mount(Button("Apply", id="pattern-apply", variant="primary"))
            self._pattern_section_path = path
            return
        if kind == "manual-add":
            self.current_path = None
            await editor.mount(Static("Add custom key", classes="field-label"))
            await editor.mount(Static(
                "Type a key name and value, then press Apply. Use dots for nested "
                "keys (e.g. 'ui.theme' creates {ui: {theme: ...}}).",
                classes="field-desc",
            ))
            await editor.mount(Input(placeholder="key name (dotted for nested)", id="manual-key"))
            await editor.mount(Input(placeholder="value", id="manual-value"))
            await editor.mount(Button("Apply", id="manual-apply", variant="primary"))
            return
        if kind not in ("leaf", "ghost-leaf") or self.doc is None or self.index is None:
            await editor.mount(Static("Select a leaf to edit its value.", classes="placeholder"))
            self.current_path = None
            return
        self.current_path = path
        self._mount_editor(editor, path, is_ghost=(kind == "ghost-leaf"))

    def _mount_editor(self, container: VerticalScroll, path: Path_, is_ghost: bool = False) -> None:
        assert self.doc is not None and self.index is not None
        spec = self.index.get(path)
        if is_ghost:
            value = spec.default  # may be None — widgets handle that
            self.current_default = spec.default
        else:
            value = self.doc.get(path)
            self.current_default = None  # not a ghost; promotion gating doesn't apply
        self.current_is_ghost = is_ghost

        suffix = "  (unset — editing default)" if is_ghost else ""
        container.mount(Label(".".join(path) + suffix, classes="field-label"))
        if path in self.index.deprecated:
            container.mount(Static("⚠ Deprecated.", classes="field-warning"))
        if spec.description:
            container.mount(Static(spec.description, classes="field-desc"))

        widget = self._make_widget(spec, value)
        container.mount(widget)

        # Non-authoritative dropdowns (web scraping / key-name heuristics) may
        # miss values the docs don't enumerate explicitly — let the user type
        # one anyway. `spec.warning` is set on exactly these sources, so it's
        # the right gate. Schema-driven enums never get this escape hatch.
        if spec.widget == "select" and spec.warning:
            container.mount(Static("Value not in the list? Type a custom value:", classes="field-meta"))
            initial = "" if (value is None or value in (spec.enum or [])) else str(value)
            container.mount(Input(value=initial, placeholder="custom value", id="custom-value"))

        meta_bits = [f"widget: {spec.widget}", f"source: {spec.source}"]
        if spec.enum:
            meta_bits.append(f"options: {len(spec.enum)}")
        if spec.minimum is not None or spec.maximum is not None:
            meta_bits.append(f"range: {spec.minimum}..{spec.maximum}")
        if spec.default is not None:
            meta_bits.append(f"default: {spec.default!r}")
        container.mount(Static(" · ".join(meta_bits), classes="field-meta"))
        if spec.warning:
            container.mount(Static(f"⚠ {spec.warning}", classes="field-warning"))
        if is_ghost:
            container.mount(Static(
                "Ghost key: change the value to anything other than the default to add it to the file.",
                classes="field-meta",
            ))

    def _make_widget(self, spec: FieldSpec, value: Any):
        widget = spec.widget
        if widget == "switch":
            return Switch(value=bool(value), id="editor-widget")
        if widget == "color":
            return HexColorPicker(value=str(value) if value else "#000000", id="editor-widget")
        if widget == "select" and spec.enum:
            options = [(str(o), o) for o in spec.enum]
            return Select(options=options, value=value if value in spec.enum else Select.NULL, id="editor-widget")
        if widget == "number":
            return Input(value=str(value) if value is not None else "", type="number", id="editor-widget")
        if widget == "list":
            return ListEditor(
                items=value or [],
                item_type=spec.item_type,
                item_enum=spec.item_enum,
                id="editor-widget",
            )
        return Input(value="" if value is None else str(value), id="editor-widget")

    # --- Change handlers (each writes back to the doc) -----------------------

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if self.current_path is None or self.doc is None:
            return
        self._commit(self.current_path, event.value)

    def on_select_changed(self, event: Select.Changed) -> None:
        if self.current_path is None or self.doc is None or event.value is Select.NULL:
            return
        self._commit(self.current_path, event.value)

    def on_input_changed(self, event: Input.Changed) -> None:
        if self.current_path is None or self.doc is None or self.index is None:
            return
        # Custom-value override for non-authoritative dropdowns: free-form
        # string commit, no type coercion (the field is text-typed by
        # construction — see _mount_editor's gate).
        if event.input.id == "custom-value":
            self._commit(self.current_path, event.value)
            return
        # Only handle the top-level editor Input. HexColorPicker / ListEditor
        # consume their own internal Input events and emit their own Changed.
        if event.input.id != "editor-widget":
            return
        spec = self.index.get(self.current_path)
        raw = event.value
        if spec.widget == "number":
            try:
                new_val = int(raw) if spec.is_integer else float(raw)
            except ValueError:
                return
            self._commit(self.current_path, new_val)
        else:
            self._commit(self.current_path, raw)

    def on_hex_color_picker_changed(self, event: HexColorPicker.Changed) -> None:
        if self.current_path is None or self.doc is None:
            return
        self._commit(self.current_path, event.value)

    def on_list_editor_changed(self, event: ListEditor.Changed) -> None:
        if self.current_path is None or self.doc is None:
            return
        self._commit(self.current_path, event.items)

    def _commit(self, path: Path_, value: Any) -> None:
        assert self.doc is not None and self.index is not None
        # Ghost promotion rule: only write to the doc when the value differs
        # from the schema default. This protects against accidentally adding a
        # key the user only browsed past.
        if self.current_is_ghost and path == self.current_path:
            if value == self.current_default:
                self._set_status("ghost unchanged — value matches default, not writing")
                return
            created = self.doc.add_path(path, value)
            self.current_is_ghost = False
            # Move the spec from ghost to real so meta says 'set' not 'unset'.
            if path in self.index.ghost_specs:
                self.index.specs[path] = self.index.ghost_specs.pop(path)
            created_msg = ""
            if created:
                created_msg = "  ·  created: " + ", ".join(".".join(p) for p in created)
            self.dirty = True
            self._refresh_tree_full()
            self._set_status(f"added {'.'.join(path)} = {value!r}{created_msg}")
            return
        self.doc.set(path, value)
        self.dirty = True
        self._refresh_tree_label(path)
        self._set_status(f"edited {'.'.join(path)} — unsaved changes")

    def _refresh_tree_full(self) -> None:
        """Rebuild the entire tree — used after structural changes (add key,
        toggle unset). Cheaper than tracking individual node changes for now."""
        if self.doc is None:
            return
        tree = self.query_one(Tree)
        tree.clear()
        tree.root.label = self.source_path.name
        self._populate(tree.root, self.doc.data, ())
        tree.root.expand()

    def _refresh_tree_label(self, path: Path_) -> None:
        tree = self.query_one(Tree)
        node = tree.root
        for part in path[:-1]:
            match = next((c for c in node.children if c.data and c.data[1] == path[: len(c.data[1])]), None)
            if match is None:
                return
            node = match
        for child in node.children:
            if child.data and child.data[1] == path:
                child.set_label(self._leaf_label(path[-1], self.doc.get(path)))
                return

    # --- Actions -------------------------------------------------------------

    def action_save(self) -> None:
        if self.doc is None:
            return
        try:
            backup = self.doc.save()
            self.dirty = False
            if backup:
                self._set_status(f"saved {self.source_path}  ·  backup: {backup}")
            else:
                self._set_status(f"saved {self.source_path}  ·  no prior file to back up")
        except Exception as exc:
            self._set_status(f"save failed: {exc}", error=True)

    def action_reload(self) -> None:
        self._reload()

    def action_toggle_unset(self) -> None:
        """Ctrl+U — show or hide schema-declared keys that aren't in the file."""
        if self.index is None or not self.index.ghost_specs:
            self._set_status("no schema-declared unset keys for this file")
            return
        self.show_unset = not self.show_unset
        self._refresh_tree_full()
        state = "ON" if self.show_unset else "OFF"
        n = len(self.index.ghost_specs)
        skipped = self.index.skipped_conditionals
        skip_note = f"  ({skipped} conditional branches skipped)" if skipped else ""
        self._set_status(f"show unset: {state}  ·  {n} ghost keys{skip_note}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self.doc is None:
            return
        if event.button.id == "manual-apply":
            key_input = self.query_one("#manual-key", Input).value.strip()
            val_input = self.query_one("#manual-value", Input).value
            if not key_input:
                self._set_status("custom key: name is empty", error=True)
                return
            new_path = tuple(part for part in key_input.split(".") if part)
            if not new_path:
                self._set_status("custom key: name is empty", error=True)
                return
            self._apply_custom_add(new_path, val_input)
            event.stop()
            return
        if event.button.id != "pattern-apply" or self._pattern_section_path is None:
            return
        key_input = self.query_one("#pattern-key", Input).value.strip()
        val_input = self.query_one("#pattern-value", Input).value
        if not key_input:
            self._set_status("custom key: name is empty", error=True)
            return
        new_path = self._pattern_section_path + (key_input,)
        self._apply_custom_add(new_path, val_input)
        event.stop()

    def _apply_custom_add(self, new_path: Path_, val_input: str) -> None:
        assert self.doc is not None
        try:
            created = self.doc.add_path(new_path, val_input)
        except Exception as exc:
            self._set_status(f"add failed: {exc}", error=True)
            return
        self.dirty = True
        self._refresh_tree_full()
        msg = f"added custom key {'.'.join(new_path)} = {val_input!r}"
        if created:
            msg += "  ·  created: " + ", ".join(".".join(p) for p in created)
        self._set_status(msg)

    def _set_status(self, msg: str, error: bool = False) -> None:
        bar = self.query_one("#status", Static)
        bar.update(msg)
        bar.styles.color = "red" if error else "white"


def run(source_path: Path, web_urls: list[str] | None = None) -> None:
    EzConfigApp(source_path, web_urls=web_urls).run()
