"""The Textual app: key tree on the left, editor on the right."""
from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any, NamedTuple

from rich.syntax import Syntax
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import Button, Footer, Header, Input, Label, Static, Switch, Tree
from textual.widgets.tree import TreeNode

from ezconfig import formats, schema as schema_mod
from ezconfig.document import Document, KeyPath, is_container, items, parse_literal
from ezconfig.index import Index, build
from ezconfig.spec import COLOR_RE, FieldSpec, infer
from ezconfig.widgets import ChoiceEditor, ColorPicker, ListEditor, option_label

MAX_MATCHES = 500


class Node(NamedTuple):
    kind: str  # section | key | unset-section | unset-key | add | more
    path: KeyPath


def dotted(path: KeyPath) -> str:
    return ".".join(path) or "(top level)"


def show_value(value: Any, limit: int = 40) -> str:
    if isinstance(value, list):
        return f"[{len(value)} items]"
    if isinstance(value, dict):
        return f"{{{len(value)} keys}}"
    s = option_label(value)
    return s if len(s) <= limit else s[: limit - 1] + "…"


class ReviewScreen(ModalScreen[None]):
    """Unsaved changes as a diff, plus schema problems."""

    BINDINGS = [Binding("escape,ctrl+o", "dismiss", "Close")]
    DEFAULT_CSS = """
    ReviewScreen { align: center middle; }
    ReviewScreen > VerticalScroll { width: 90%; height: 90%; border: round $accent; background: $surface; padding: 0 1; }
    ReviewScreen .title { text-style: bold; padding-top: 1; }
    """

    def __init__(self, diff: str, problems: list[str]):
        super().__init__()
        self.diff = diff
        self.problems = problems

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("Changes", classes="title")
            if self.diff:
                yield Static(Syntax(self.diff, "diff", theme="ansi_dark", word_wrap=True))
            else:
                yield Static("No changes.")
            yield Static("Schema check", classes="title")
            yield Static("\n".join(self.problems) or "No problems found.", markup=False)
            yield Static("Esc closes.", classes="title")


class EzConfigApp(App[None]):
    AUTO_FOCUS = "#tree"
    CSS = """
    #banner { height: auto; padding: 0 1; background: $warning; color: $background; display: none; }
    #banner.visible { display: block; }
    #main { height: 1fr; }
    #tree-pane { width: 40%; border-right: solid $accent; }
    #find { display: none; }
    #find.visible { display: block; }
    #tree { padding: 0 1; }
    #editor { width: 1fr; padding: 1 2; }
    #status { height: auto; max-height: 3; padding: 0 1; background: $boost; }
    #status.error { color: $error; }
    .path { text-style: bold; }
    .muted { color: $text-muted; padding-top: 1; }
    .warn { color: $warning; padding-top: 1; }
    .desc { padding-bottom: 1; }
    """

    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("ctrl+o", "review", "Review"),
        Binding("ctrl+f", "find", "Find"),
        Binding("ctrl+t", "toggle_unset", "Unset keys"),
        Binding("delete", "remove", "Remove key"),
        Binding("ctrl+r", "reload", "Reload"),
        Binding("escape", "back", "Back to tree", show=False),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, target: Path, docs: list[str] | None = None, schema: str | None = None):
        super().__init__()
        self.target = target
        self.docs = list(docs or [])
        self.schema = schema
        self.doc: Document | None = None
        self.index: Index | None = None
        self.current: Node | None = None  # key shown in the editor
        self.nodes: dict[KeyPath, TreeNode[Node]] = {}
        self.unset_children: dict[KeyPath, set[str]] = {}
        self.show_unset = False
        self.dirty = False
        self.armed = ""  # action that needs a second press to discard changes
        self.add_prefix: KeyPath = ()  # section the add form adds to

    # --- Layout ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="banner")
        with Horizontal(id="main"):
            with Vertical(id="tree-pane"):
                yield Input(placeholder="find key (Esc to close)", id="find")
                yield Tree[Node]("", id="tree")
            yield VerticalScroll(id="editor")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"ezconfig: {self.target.name}"
        self.load()

    # --- Loading ---------------------------------------------------------

    @work(thread=True, exclusive=True, group="load")
    def load(self) -> None:
        sources = "schema and docs" if self.docs else "schema"
        self.call_from_thread(self.set_status, f"loading {self.target} (looking up {sources})…")
        try:
            doc = formats.load(self.target) if self.target.exists() else formats.new(self.target)
            index = build(doc, self.docs, self.schema)
        except Exception as exc:
            self.call_from_thread(self.set_status, f"can't load {self.target}: {exc}", True)
            return
        self.call_from_thread(self._loaded, doc, index)

    def _loaded(self, doc: Document, index: Index) -> None:
        self.doc, self.index = doc, index
        self.dirty = False
        self.current = None
        self.query_one("#editor", VerticalScroll).remove_children()
        notes = list(doc.notes)
        if not self.target.exists():
            notes.append("New file: it is created on save.")
        banner = self.query_one("#banner", Static)
        banner.update(Text("\n".join(notes)))
        banner.set_class(bool(notes), "visible")
        self.rebuild_tree(keep=False)
        unset = f"{len(index.unset)} unset keys (ctrl+t)" if index.unset else "no unset keys"
        self.set_status(f"{self.target} ({doc.format_name}): {len(index.specs)} keys, {unset}. " + "; ".join(index.sources))

    # --- Tree ------------------------------------------------------------

    def _container(self, path: KeyPath) -> Any:
        assert self.doc is not None
        try:
            value = self.doc.get(path)
        except (KeyError, IndexError, ValueError, TypeError):
            return None
        return value if is_container(value) else None

    def rebuild_tree(self, keep: bool = True) -> None:
        if self.doc is None or self.index is None:
            return
        tree = self.query_one("#tree", Tree)
        expanded = [p for p, n in self.nodes.items() if n.is_expanded] if keep else []
        cursor = tree.cursor_node.data.path if keep and tree.cursor_node and tree.cursor_node.data else None
        self.unset_children = {}
        for path in [*self.index.unset, *self.index.sections]:
            for i in range(len(path)):
                self.unset_children.setdefault(path[:i], set()).add(path[i])
        tree.clear()
        self.nodes = {}
        tree.root.set_label(Text(self.target.name, style="bold"))
        tree.root.data = Node("section", ())
        find = self.query_one("#find", Input)
        if find.has_class("visible") and find.value.strip():
            self._fill_matches(tree.root, find.value.strip().lower())
        else:
            self._fill(tree.root, ())
        tree.root.expand()
        for path in sorted(expanded, key=len):
            if node := self.nodes.get(path):
                self._expand(node)
        if cursor and (node := self.nodes.get(cursor)):
            tree.move_cursor(node)

    def _fill(self, node: TreeNode[Node], prefix: KeyPath) -> None:
        assert self.doc is not None and self.index is not None
        container = self._container(prefix)
        present: set[str] = set()
        for key, value in items(container) if container is not None else ():
            path = prefix + (key,)
            present.add(key)
            if is_container(value):
                self.nodes[path] = child = node.add(self._section_label(key, path, unset=False), Node("section", path))
                child.add_leaf("…", Node("more", path))
            else:
                self.nodes[path] = node.add_leaf(self._key_label(key, path, value), Node("key", path))
        if self.show_unset:
            for key in sorted(self.unset_children.get(prefix, set()) - present):
                path = prefix + (key,)
                if path in self.index.unset and path not in self.unset_children:
                    spec = self.index.unset[path]
                    self.nodes[path] = node.add_leaf(self._key_label(key, path, spec.default, unset=True), Node("unset-key", path))
                else:
                    self.nodes[path] = child = node.add(self._section_label(key, path, unset=True), Node("unset-section", path))
                    child.add_leaf("…", Node("more", path))
        if self.doc.can_add and (container is None or isinstance(container, dict)):
            node.add_leaf(Text("+ Add key", style="italic dim"), Node("add", prefix))

    def _fill_matches(self, root: TreeNode[Node], query: str) -> None:
        assert self.doc is not None and self.index is not None
        paths = [(p, "key") for p in self.index.specs] + [(p, "unset-key") for p in self.index.unset]
        shown = 0
        for path, kind in sorted(paths):
            if query not in dotted(path).lower():
                continue
            value = self.doc.get(path) if kind == "key" else self.index.unset[path].default
            self.nodes[path] = root.add_leaf(self._key_label(dotted(path), path, value, unset=kind == "unset-key"), Node(kind, path))
            shown += 1
            if shown >= MAX_MATCHES:
                root.add_leaf(Text(f"(first {MAX_MATCHES} matches)", style="dim"), Node("more", ()))
                break

    def _fill_lazy(self, node: TreeNode[Node]) -> None:
        """Replace a section's "…" placeholder with its children."""
        children = list(node.children)
        if children and children[0].data and children[0].data.kind == "more" and node.data:
            node.remove_children()
            self._fill(node, node.data.path)

    def _expand(self, node: TreeNode[Node]) -> None:
        self._fill_lazy(node)
        node.expand()

    def _section_label(self, key: str, path: KeyPath, unset: bool) -> Text:
        text = Text(f"{key}/", style="dim italic" if unset else "")
        if self.show_unset and self.index is not None:
            below = sum(1 for p in self.index.unset if p[: len(path)] == path)
            if below:
                text.append(f"  {below} unset", style="dim")
        return text

    def _key_label(self, key: str, path: KeyPath, value: Any, unset: bool = False) -> Text:
        spec = self.index.get(path) if self.index else FieldSpec()
        text = Text(f"{key}: ", style="dim italic" if unset else "")
        if isinstance(value, str) and COLOR_RE.match(value):
            text.append("■ ", style=self._swatch_style(value))
        text.append(show_value(value) if value is not None else "(no default)", style="dim italic" if unset else "")
        if spec.deprecated:
            text.append("  deprecated", style="red")
        return text

    @staticmethod
    def _swatch_style(value: str) -> str:
        digits = value[1:] if value.startswith("#") else value[2:]
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        return f"#{digits[:6]}"

    def on_tree_node_expanded(self, event: Tree.NodeExpanded[Node]) -> None:
        self._fill_lazy(event.node)

    def refresh_label(self, path: KeyPath) -> None:
        node = self.nodes.get(path)
        if node is not None and self.doc is not None and node.data and node.data.kind == "key":
            key = dotted(path) if self.query_one("#find", Input).has_class("visible") else path[-1]
            node.set_label(self._key_label(key, path, self.doc.get(path)))

    # --- Editor ----------------------------------------------------------

    async def on_tree_node_selected(self, event: Tree.NodeSelected[Node]) -> None:
        if event.node.data is not None:
            await self.show_editor(event.node.data)

    async def show_editor(self, node: Node) -> None:
        pane = self.query_one("#editor", VerticalScroll)
        await pane.remove_children()
        if self.doc is None or self.index is None:
            return
        self.current = None
        if node.kind == "add":
            self.add_prefix = node.path
            await pane.mount_all(self._add_form(node.path))
            self._focus_editor()
            return
        if node.kind not in ("key", "unset-key"):
            await pane.mount(Static("Select a key to edit it.", classes="muted"))
            return
        self.current = node
        spec = self.index.get(node.path)
        unset = node.kind == "unset-key"
        value = spec.default if unset else self.doc.get(node.path)
        parts: list[Any] = [Label(Text(dotted(node.path) + ("  (unset)" if unset else "")), classes="path")]
        if spec.deprecated:
            parts.append(Static(Text(f"Deprecated: {spec.deprecated}"), classes="warn"))
        if spec.description:
            parts.append(Static(Text(spec.description), classes="desc"))
        parts.append(self._editor_widget(spec, value))
        meta = [f"source: {spec.source}"]
        if spec.options:
            meta.append(f"{len(spec.options)} options" + ("" if spec.strict else ", other values allowed"))
        if spec.minimum is not None or spec.maximum is not None:
            meta.append(f"range: {spec.minimum if spec.minimum is not None else ''}..{spec.maximum if spec.maximum is not None else ''}")
        if spec.default is not None:
            meta.append(f"default: {option_label(spec.default)}")
        parts.append(Static(Text(" · ".join(meta)), classes="muted"))
        if spec.warning:
            parts.append(Static(Text(spec.warning), classes="warn"))
        if unset:
            parts.append(Static("Unset key: a value other than the default adds it to the file.", classes="muted"))
        await pane.mount_all(parts)
        self._focus_editor()

    def _focus_editor(self) -> None:
        pane = self.query_one("#editor", VerticalScroll)
        target = next((w for w in pane.query("*") if w.focusable), None)
        if target is not None:
            target.focus()

    def _editor_widget(self, spec: FieldSpec, value: Any) -> Any:
        kind = spec.widget
        if kind == "switch":
            return Switch(value=bool(value), id="editor-input")
        if kind == "color":
            shown = "#" + value[2:] if isinstance(value, str) and value.startswith("0x") else value
            return ColorPicker(value=str(shown) if shown else "#000000", id="editor-input")
        if kind == "choice" and spec.options:
            return ChoiceEditor(spec.options, value, strict=spec.strict, notes=spec.option_notes, id="editor-input")
        if kind == "number":
            return Input(value="" if value is None else str(value), type="integer" if spec.integer else "number", id="editor-input")
        if kind == "list":
            return ListEditor(value if isinstance(value, list) else [], spec.item_type, spec.item_options, id="editor-input")
        if kind == "readonly":
            return Static(Text(json.dumps(value, indent=2, default=str) + "\n\nThis value can't be edited here."), classes="muted")
        return Input(value="" if value is None else str(value), id="editor-input")

    def _add_form(self, prefix: KeyPath) -> list[Any]:
        assert self.index is not None
        n = len(prefix)
        known = sorted(".".join(p[n:]) for p in self.index.unset if p[:n] == prefix)
        where = dotted(prefix)
        hint = "Use dots for nested keys, e.g. ui.theme." if self.doc and self.doc.format_name != "text" else ""
        return [
            Label(f"Add key in {where}", classes="path"),
            Static(f"Values are typed: true, 42, 1.5; quote to force text. {hint}", classes="desc"),
            Input(placeholder="key", id="add-key", suggester=SuggestFromList(known, case_sensitive=False) if known else None),
            Input(placeholder="value", id="add-value"),
            Button("Add", id="add-apply", variant="primary"),
        ]

    # --- Edits -----------------------------------------------------------

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "editor-input":
            self.commit(event.value)

    def on_choice_editor_changed(self, event: ChoiceEditor.Changed) -> None:
        self.commit(event.value)

    def on_color_picker_changed(self, event: ColorPicker.Changed) -> None:
        if self.current and self.doc and self.current.kind == "key":
            old = self.doc.get(self.current.path)
            if isinstance(old, str) and old.startswith("0x"):
                self.commit("0x" + event.value[1:])
                return
        self.commit(event.value)

    def on_list_editor_changed(self, event: ListEditor.Changed) -> None:
        self.commit(event.items)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "find":
            self.rebuild_tree(keep=False)
            return
        if event.input.id != "editor-input" or self.current is None or self.index is None:
            return
        spec = self.index.get(self.current.path)
        if spec.widget != "number":
            self.commit(event.value)
            return
        try:
            number = int(event.value) if spec.integer else float(event.value)
        except ValueError:
            return
        if (spec.minimum is not None and number < spec.minimum) or (spec.maximum is not None and number > spec.maximum):
            self.set_status(f"{number} is out of range {spec.minimum}..{spec.maximum}; not applied", True)
            return
        self.commit(number)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "find":
            tree = self.query_one("#tree", Tree)
            tree.focus()
            first = next((n for n in tree.root.children if n.data and n.data.kind in ("key", "unset-key")), None)
            if first is not None:
                tree.select_node(first)
        elif event.input.id in ("add-key", "add-value"):
            self.add_key()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add-apply":
            self.add_key()

    def commit(self, value: Any) -> None:
        if self.current is None or self.doc is None or self.index is None:
            return
        path = self.current.path
        self.armed = ""
        try:
            if self.current.kind == "unset-key":
                if value == self.index.get(path).default:
                    self.set_status(f"{dotted(path)}: same as the default, not added")
                    return
                created = self.doc.add(path, value)
                self.index.mark_set(path)
                self.current = Node("key", path)
                self.dirty = True
                self.rebuild_tree()
                extra = f" (created {', '.join(dotted(p) for p in created)})" if created else ""
                self.set_status(f"added {dotted(path)} = {option_label(value)}{extra}; unsaved")
                return
            self.doc.set(path, value)
        except Exception as exc:
            self.set_status(f"can't set {dotted(path)}: {exc}", True)
            return
        self.dirty = True
        self.refresh_label(path)
        self.set_status(f"{dotted(path)} = {show_value(value, 60)}; unsaved")

    def add_key(self) -> None:
        if self.doc is None or self.index is None:
            return
        tree = self.query_one("#tree", Tree)
        prefix = self.add_prefix
        name = self.query_one("#add-key", Input).value.strip()
        raw = self.query_one("#add-value", Input).value
        rel = tuple(p for p in name.split(".") if p) if self.doc.format_name != "text" else (name,)
        if not name or not rel:
            self.set_status("add: the key name is empty", True)
            return
        path = prefix + rel
        if self.doc.has(path):
            self.set_status(f"add: {dotted(path)} already exists", True)
            return
        spec = self.index.unset.get(path)
        value = raw if spec and spec.widget in ("text", "choice") and not raw.strip().startswith(('"', "'")) else parse_literal(raw)
        try:
            created = self.doc.add(path, value)
        except Exception as exc:
            self.set_status(f"add: {exc}", True)
            return
        self.index.mark_set(path)
        self.index.specs.setdefault(path, infer(value))
        self.dirty = True
        self.rebuild_tree()
        extra = f" (created {', '.join(dotted(p) for p in created)})" if created else ""
        self.set_status(f"added {dotted(path)} = {option_label(value)}{extra}; unsaved")
        if target := self.nodes.get(path):
            tree.move_cursor(target)

    # --- Actions ---------------------------------------------------------

    def action_save(self) -> None:
        if self.doc is None:
            return
        try:
            copy = self.doc.save()
        except Exception as exc:
            self.set_status(f"save failed: {exc}", True)
            return
        self.dirty = False
        self.armed = ""
        self.query_one("#banner", Static).set_class(bool(self.doc.notes), "visible")
        self.set_status(f"saved {self.target}" + (f"; backup: {copy}" if copy else ""))
        if self.index and self.index.schemas:
            self.check_after_save()

    @work(thread=True, exclusive=True, group="check")
    def check_after_save(self) -> None:
        problems = self._problems()
        if problems:
            self.call_from_thread(self.set_status, f"saved {self.target}; {len(problems)} schema problems (ctrl+o to review)", True)

    def _problems(self) -> list[str]:
        if self.doc is None or self.index is None:
            return []
        data = self.doc.plain()
        out: list[str] = []
        for s in self.index.schemas:
            for line in schema_mod.validate(data, s):
                if line not in out:
                    out.append(line)
        return out

    @work(thread=True, exclusive=True, group="check")
    def action_review(self) -> None:
        if self.doc is None:
            return
        before = self.doc.original_text.splitlines(keepends=True)
        after = self.doc.dumps().splitlines(keepends=True)
        diff = "".join(difflib.unified_diff(before, after, "on disk", "unsaved"))
        problems = self._problems() if self.index and self.index.schemas else ["No schema for this file."]
        self.call_from_thread(self.push_screen, ReviewScreen(diff, problems))

    def action_find(self) -> None:
        find = self.query_one("#find", Input)
        find.add_class("visible")
        find.focus()

    def action_back(self) -> None:
        """Esc: close find, or move focus from the editor back to the tree."""
        find = self.query_one("#find", Input)
        if find.has_class("visible"):
            find.value = ""
            find.remove_class("visible")
            self.rebuild_tree(keep=False)
        self.query_one("#tree", Tree).focus()

    def action_toggle_unset(self) -> None:
        if self.index is None:
            return
        if not self.index.unset and not self.show_unset:
            self.set_status("no unset keys: no schema or docs list keys missing from this file")
            return
        self.show_unset = not self.show_unset
        self.rebuild_tree()
        skipped = f"; {self.index.skipped} branching schema sections not listed" if self.index.skipped else ""
        state = "shown" if self.show_unset else "hidden"
        self.set_status(f"unset keys {state}: {len(self.index.unset)}{skipped}")

    def action_remove(self) -> None:
        if self.doc is None or self.index is None:
            return
        node = self.query_one("#tree", Tree).cursor_node
        if node is None or node.data is None or node.data.kind not in ("key", "section") or not node.data.path:
            self.set_status("select a key or section in the tree to remove it")
            return
        path = node.data.path
        try:
            self.doc.delete(path)
        except Exception as exc:
            self.set_status(f"can't remove {dotted(path)}: {exc}", True)
            return
        for p in [p for p in self.index.specs if p[: len(path)] == path]:
            self.index.mark_unset(p)
        self.dirty = True
        self.rebuild_tree()
        if self.current and self.current.path[: len(path)] == path:
            self.query_one("#editor", VerticalScroll).remove_children()
            self.current = None
        self.set_status(f"removed {dotted(path)}; unsaved (ctrl+o to review, ctrl+r to discard)")

    def _confirm_discard(self, action: str, verb: str) -> bool:
        if not self.dirty or self.armed == action:
            return True
        self.armed = action
        self.set_status(f"unsaved changes: ctrl+s saves, {verb} again discards them", True)
        return False

    def action_reload(self) -> None:
        if self._confirm_discard("reload", "ctrl+r"):
            self.armed = ""
            self.load()

    async def action_quit(self) -> None:
        if self._confirm_discard("quit", "ctrl+q"):
            self.exit()

    def set_status(self, message: str, error: bool = False) -> None:
        status = self.query_one("#status", Static)
        status.update(Text(message))
        status.set_class(error, "error")


def run(target: Path, docs: list[str] | None = None, schema: str | None = None) -> None:
    EzConfigApp(target, docs=docs, schema=schema).run()
