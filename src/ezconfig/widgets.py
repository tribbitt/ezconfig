"""Editor widgets: color picker, option chooser, list editor."""
from __future__ import annotations

import colorsys
import json
import re
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Input, OptionList, Select, Static, Switch
from textual.widgets.option_list import Option

HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def _hsl_to_hex(hue: float, lightness: float, saturation: float = 1.0) -> str:
    r, g, b = colorsys.hls_to_rgb((hue % 360) / 360.0, lightness, saturation)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def _rgb(hex_value: str) -> tuple[int, int, int]:
    if not HEX_RE.match(hex_value):
        return (0, 0, 0)
    v = hex_value[1:]
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


_HUES = list(range(0, 360, 15))
_LIGHTNESS = [i / 10 for i in range(11)]


class ColorPicker(Widget):
    """Hex input, RGB inputs, a hue strip and a lightness strip, all in sync.
    Posts Changed with a #rrggbb value (alpha kept for #rrggbbaa input)."""

    DEFAULT_CSS = """
    ColorPicker { height: auto; }
    ColorPicker .row { height: 3; }
    ColorPicker #hex { width: 1fr; }
    ColorPicker #swatch { width: 10; height: 3; border: solid $foreground; margin-left: 1; }
    ColorPicker .rgb-label { width: 3; content-align: center middle; height: 3; }
    ColorPicker .rgb { width: 1fr; margin-right: 1; }
    ColorPicker .strip-label { color: $text-muted; padding-top: 1; }
    ColorPicker .strip { height: 2; }
    ColorPicker .cell { width: 1fr; min-width: 2; height: 2; border: none; }
    """

    class Changed(Message):
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    def __init__(self, value: str = "#000000", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.value = value if HEX_RE.match(value) else "#000000"
        self._syncing = False

    def compose(self) -> ComposeResult:
        r, g, b = _rgb(self.value)
        with Horizontal(classes="row"):
            yield Input(value=self.value, placeholder="#rrggbb", id="hex")
            yield Static("", id="swatch")
        with Horizontal(classes="row"):
            for label, val in (("R", r), ("G", g), ("B", b)):
                yield Static(label, classes="rgb-label")
                yield Input(value=str(val), type="integer", id=f"rgb-{label.lower()}", classes="rgb")
        yield Static("hue", classes="strip-label")
        with Horizontal(classes="strip"):
            for hue in _HUES:
                cell = Button(" ", id=f"hue-{hue}", classes="cell")
                cell.styles.background = _hsl_to_hex(hue, 0.5)
                yield cell
        yield Static("lightness", classes="strip-label")
        with Horizontal(classes="strip", id="lightness"):
            for i in range(len(_LIGHTNESS)):
                yield Button(" ", id=f"light-{i}", classes="cell")

    def on_mount(self) -> None:
        self._show(self.value)

    def _hue(self) -> float:
        r, g, b = _rgb(self.value)
        return colorsys.rgb_to_hls(r / 255, g / 255, b / 255)[0] * 360

    def _show(self, value: str) -> None:
        swatch = self.query_one("#swatch", Static)
        if HEX_RE.match(value):
            swatch.styles.background = "#" + "".join(f"{c:02x}" for c in _rgb(value))
            swatch.update("")
        else:
            swatch.update("?")
        hue = self._hue()
        for i, light in enumerate(_LIGHTNESS):
            self.query_one(f"#light-{i}", Button).styles.background = _hsl_to_hex(hue, light)

    def _set(self, value: str, source: str) -> None:
        alpha = self.value[7:9] if len(self.value) == 9 and source != "hex" else ""
        self.value = value + alpha
        self._syncing = True
        try:
            if source != "hex":
                self.query_one("#hex", Input).value = self.value
            if source != "rgb":
                for name, val in zip("rgb", _rgb(self.value)):
                    self.query_one(f"#rgb-{name}", Input).value = str(val)
        finally:
            self._syncing = False
        self._show(self.value)
        self.post_message(self.Changed(self.value))

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        if self._syncing:
            return
        if event.input.id == "hex":
            if HEX_RE.match(event.value):
                self._set(event.value, "hex")
            else:
                self.query_one("#swatch", Static).update("?")
            return
        try:
            r, g, b = (max(0, min(255, int(self.query_one(f"#rgb-{n}", Input).value or 0))) for n in "rgb")
        except ValueError:
            return
        self._set(f"#{r:02x}{g:02x}{b:02x}", "rgb")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        bid = event.button.id or ""
        if bid.startswith("hue-"):
            self._set(_hsl_to_hex(int(bid[4:]), 0.5), "strip")
        elif bid.startswith("light-"):
            self._set(_hsl_to_hex(self._hue(), _LIGHTNESS[int(bid[6:])]), "strip")


def option_label(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


class ChoiceEditor(Widget):
    """Pick one of `options`: type to filter, Enter or click to choose.
    When `strict` is False, Enter also accepts text that isn't an option."""

    DEFAULT_CSS = """
    ChoiceEditor { height: auto; }
    ChoiceEditor OptionList { height: auto; max-height: 16; }
    """
    BINDINGS = [Binding("down", "focus_list", "Options", show=False)]

    class Changed(Message):
        def __init__(self, value: Any) -> None:
            self.value = value
            super().__init__()

    def __init__(
        self,
        options: list[Any],
        value: Any,
        strict: bool = True,
        notes: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.options = list(options)
        self.notes = notes or []
        self.strict = strict
        self.value = value
        self._initial = "" if value is None else option_label(value)

    def compose(self) -> ComposeResult:
        hint = "type to filter" if self.strict else "type to filter, or enter any value"
        yield Input(value=self._initial, placeholder=hint, id="choice-input")
        yield OptionList(id="choice-list")

    def on_mount(self) -> None:
        self._fill("")

    def _fill(self, query: str) -> None:
        olist = self.query_one(OptionList)
        olist.clear_options()
        q = query.lower()
        current = None
        for i, opt in enumerate(self.options):
            label = option_label(opt)
            note = self.notes[i] if i < len(self.notes) else ""
            if q and q not in label.lower() and q not in note.lower():
                continue
            text = Text(label)
            if note:
                text.append("  " + note.splitlines()[0][:80], style="dim")
            if opt == self.value and current is None:
                current = olist.option_count
            olist.add_option(Option(text, id=str(i)))
        if current is not None:
            olist.highlighted = current

    def action_focus_list(self) -> None:
        self.query_one(OptionList).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._fill("" if event.value == self._initial else event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        text = event.value
        exact = [o for o in self.options if option_label(o) == text]
        if exact:
            self._choose(exact[0])
            return
        olist = self.query_one(OptionList)
        if olist.option_count == 1:
            self._choose(self.options[int(olist.get_option_at_index(0).id or 0)])
        elif not self.strict and text:
            self._choose(text)
        else:
            self.notify("Not one of the options.", severity="warning")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._choose(self.options[int(event.option.id or 0)])

    def _choose(self, value: Any) -> None:
        self.value = value
        self._initial = option_label(value)
        self.query_one(Input).value = self._initial
        self.post_message(self.Changed(value))


class ListEditor(Widget):
    """Edit a list of scalars: one row per item, with remove and add buttons."""

    DEFAULT_CSS = """
    ListEditor { height: auto; }
    ListEditor #rows { height: auto; }
    ListEditor .row { height: 3; }
    ListEditor .row Input, ListEditor .row Select { width: 1fr; }
    ListEditor .row Switch { width: 10; }
    ListEditor .row Button { width: 5; margin-left: 1; }
    ListEditor #add { width: 14; margin-top: 1; }
    """

    class Changed(Message):
        def __init__(self, items: list[Any]) -> None:
            self.items = items
            super().__init__()

    def __init__(
        self,
        items: list[Any] | None = None,
        item_type: str = "string",
        item_options: list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.items: list[Any] = list(items or [])
        self.item_type = item_type or "string"
        self.item_options = item_options

    def compose(self) -> ComposeResult:
        yield Vertical(id="rows")
        yield Button("+ Add item", id="add", variant="primary")

    async def on_mount(self) -> None:
        await self._rebuild()

    async def _rebuild(self) -> None:
        rows = self.query_one("#rows", Vertical)
        await rows.remove_children()
        for idx, item in enumerate(self.items):
            await rows.mount(Horizontal(self._item_widget(idx, item), Button("x", id=f"rm-{idx}", variant="error"), classes="row"))

    def _item_widget(self, idx: int, item: Any) -> Widget:
        wid = f"item-{idx}"
        if self.item_options:
            options = [(option_label(o), o) for o in self.item_options]
            return Select(options, value=item if item in self.item_options else Select.NULL, id=wid)
        if self.item_type == "boolean":
            return Switch(value=bool(item), id=wid)
        if self.item_type in ("integer", "number"):
            return Input(value=str(item), type=self.item_type, id=wid)
        return Input(value="" if item is None else str(item), id=wid)

    def _changed(self) -> None:
        self.post_message(self.Changed(list(self.items)))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        bid = event.button.id or ""
        if bid == "add":
            empty = {"integer": 0, "number": 0.0, "boolean": False}.get(self.item_type, "")
            self.items.append(self.item_options[0] if self.item_options else empty)
        elif bid.startswith("rm-"):
            del self.items[int(bid[3:])]
        else:
            return
        await self._rebuild()
        self._changed()

    @staticmethod
    def _index(widget_id: str | None) -> int | None:
        if widget_id and widget_id.startswith("item-"):
            return int(widget_id[5:])
        return None

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        idx = self._index(event.input.id)
        if idx is None:
            return
        try:
            if self.item_type == "integer":
                self.items[idx] = int(event.value)
            elif self.item_type == "number":
                self.items[idx] = float(event.value)
            else:
                self.items[idx] = event.value
        except ValueError:
            return
        self._changed()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        event.stop()
        idx = self._index(event.switch.id)
        if idx is not None:
            self.items[idx] = event.value
            self._changed()

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        idx = self._index(event.select.id)
        if idx is not None and event.value is not Select.NULL:
            self.items[idx] = event.value
            self._changed()
