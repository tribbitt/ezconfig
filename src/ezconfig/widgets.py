"""Custom Textual widgets — hex color picker and list editor."""
from __future__ import annotations

import colorsys
import re
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Button, Input, Select, Static, Switch


def _hsl_to_hex(hue: float, lightness: float, saturation: float = 1.0) -> str:
    """hue in [0,360), lightness/saturation in [0,1]."""
    r, g, b = colorsys.hls_to_rgb((hue % 360) / 360.0, lightness, saturation)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


# 24-step rainbow at 50% lightness, full saturation.
_HUE_STRIP = [(h, _hsl_to_hex(h, 0.5)) for h in range(0, 360, 15)]
# 11-step lightness ramp anchors (recomputed per-hue at runtime).
_LIGHT_STEPS = [i / 10.0 for i in range(0, 11)]  # 0.0 .. 1.0 inclusive

HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


class HexColorPicker(Widget):
    """Hex + RGB color picker with live swatch preview and a preset palette.

    The hex string is the single source of truth; the RGB inputs and the
    preset buttons all read from / write to it. The widget emits its own
    `HexColorPicker.Changed` message whenever the value resolves to a valid
    hex color, so consumers don't need to handle the internal Input events.
    """

    DEFAULT_CSS = """
    HexColorPicker { height: auto; }
    HexColorPicker #hex-row { height: 3; layout: horizontal; }
    HexColorPicker #hex-row Input { width: 1fr; }
    HexColorPicker .swatch { width: 10; height: 3; border: solid white; margin-left: 1; }
    HexColorPicker #rgb-row { height: 3; layout: horizontal; margin-top: 1; }
    HexColorPicker #rgb-row .rgb-label { width: 3; content-align: center middle; }
    HexColorPicker #rgb-row .rgb-input { width: 1fr; margin-right: 1; }
    HexColorPicker .gradient-label { color: $text-muted; padding-top: 1; }
    HexColorPicker #hues { height: 2; layout: horizontal; }
    HexColorPicker .hue-cell { width: 1fr; min-width: 2; height: 2; }
    HexColorPicker #lights { height: 2; layout: horizontal; }
    HexColorPicker .light-cell { width: 1fr; min-width: 2; height: 2; }
    """

    class Changed(Message):
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    value: reactive[str] = reactive("")

    def __init__(self, value: str = "#000000", **kwargs):
        super().__init__(**kwargs)
        self.value = value if HEX_RE.match(value) else "#000000"
        self._syncing = False

    def compose(self) -> ComposeResult:
        r, g, b = self._to_rgb(self.value)
        with Horizontal(id="hex-row"):
            yield Input(value=self.value, placeholder="#rrggbb", id="hex-input")
            yield Static("", classes="swatch", id="hex-swatch")
        with Horizontal(id="rgb-row"):
            yield Static("R", classes="rgb-label")
            yield Input(value=str(r), type="integer", id="rgb-r", classes="rgb-input")
            yield Static("G", classes="rgb-label")
            yield Input(value=str(g), type="integer", id="rgb-g", classes="rgb-input")
            yield Static("B", classes="rgb-label")
            yield Input(value=str(b), type="integer", id="rgb-b", classes="rgb-input")
        yield Static("hue", classes="gradient-label")
        with Horizontal(id="hues"):
            for hue, hex_color in _HUE_STRIP:
                btn = Button(" ", id=f"hue-{hue}", classes="hue-cell")
                btn.styles.background = hex_color
                yield btn
        yield Static("lightness", classes="gradient-label")
        with Horizontal(id="lights"):
            current_hue, _ = self._current_hsl()
            for i, light in enumerate(_LIGHT_STEPS):
                btn = Button(" ", id=f"light-{i}", classes="light-cell")
                btn.styles.background = _hsl_to_hex(current_hue, light)
                yield btn

    def on_mount(self) -> None:
        self._apply_swatch(self.value)

    def _current_hsl(self) -> tuple[float, float]:
        """Derive (hue, lightness) from the current hex value. Saturation
        ignored (always treated as full)."""
        r, g, b = self._to_rgb(self.value)
        h, l, _s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
        return h * 360.0, l

    def _refresh_lightness_strip(self) -> None:
        """Repaint the lightness row to reflect the current hue."""
        current_hue, _ = self._current_hsl()
        for i, light in enumerate(_LIGHT_STEPS):
            try:
                btn = self.query_one(f"#light-{i}", Button)
            except Exception:
                return
            btn.styles.background = _hsl_to_hex(current_hue, light)

    @property
    def is_valid(self) -> bool:
        return bool(HEX_RE.match(self.value))

    # --- Event handlers (internal; everything is event.stop()'d so the
    # parent app only sees our own Changed message) -----------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._syncing:
            event.stop()
            return
        wid = event.input.id or ""
        if wid == "hex-input":
            self.value = event.value
            self._apply_swatch(event.value)
            if HEX_RE.match(event.value):
                self._sync_rgb_inputs(event.value)
                self._refresh_lightness_strip()
                self.post_message(self.Changed(event.value))
        elif wid in ("rgb-r", "rgb-g", "rgb-b"):
            new_hex = self._hex_from_rgb_inputs()
            if new_hex is None:
                event.stop()
                return
            self.value = new_hex
            self._apply_swatch(new_hex)
            self._sync_hex_input(new_hex)
            self._refresh_lightness_strip()
            self.post_message(self.Changed(new_hex))
        event.stop()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        color: str | None = None
        if bid.startswith("hue-"):
            try:
                hue = int(bid[len("hue-"):])
            except ValueError:
                return
            color = _hsl_to_hex(hue, 0.5)
        elif bid.startswith("light-"):
            try:
                idx = int(bid[len("light-"):])
            except ValueError:
                return
            current_hue, _ = self._current_hsl()
            color = _hsl_to_hex(current_hue, _LIGHT_STEPS[idx])
        else:
            return
        if color is None:
            return
        self.value = color
        self._apply_swatch(color)
        self._sync_hex_input(color)
        self._sync_rgb_inputs(color)
        # If the hue changed, repaint the lightness strip to the new hue ramp.
        if bid.startswith("hue-"):
            self._refresh_lightness_strip()
        self.post_message(self.Changed(color))
        event.stop()

    # --- Internals --------------------------------------------------------

    def _hex_from_rgb_inputs(self) -> str | None:
        try:
            r = int(self.query_one("#rgb-r", Input).value or "0")
            g = int(self.query_one("#rgb-g", Input).value or "0")
            b = int(self.query_one("#rgb-b", Input).value or "0")
        except ValueError:
            return None
        r, g, b = max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))
        return f"#{r:02x}{g:02x}{b:02x}"

    def _sync_hex_input(self, value: str) -> None:
        self._syncing = True
        try:
            self.query_one("#hex-input", Input).value = value
        finally:
            self._syncing = False

    def _sync_rgb_inputs(self, value: str) -> None:
        r, g, b = self._to_rgb(value)
        self._syncing = True
        try:
            self.query_one("#rgb-r", Input).value = str(r)
            self.query_one("#rgb-g", Input).value = str(g)
            self.query_one("#rgb-b", Input).value = str(b)
        finally:
            self._syncing = False

    @staticmethod
    def _to_rgb(hex_value: str) -> tuple[int, int, int]:
        if not HEX_RE.match(hex_value):
            return (0, 0, 0)
        v = hex_value.lstrip("#")
        if len(v) == 3:
            v = "".join(c * 2 for c in v)
        if len(v) == 8:
            v = v[:6]
        return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)

    def _apply_swatch(self, value: str) -> None:
        swatch = self.query_one("#hex-swatch", Static)
        if HEX_RE.match(value):
            v = value
            if len(v) == 4:
                v = "#" + "".join(c * 2 for c in v[1:])
            swatch.styles.background = v[:7]
            swatch.update("")
        else:
            swatch.styles.background = "black"
            swatch.update("?")


class ListEditor(Widget):
    """Edit a list of scalars. One row per item with a remove button, plus add."""

    DEFAULT_CSS = """
    ListEditor { height: auto; }
    ListEditor #rows { height: auto; }
    ListEditor .row { height: 3; layout: horizontal; margin-bottom: 0; }
    ListEditor .row Input, ListEditor .row Select { width: 1fr; }
    ListEditor .row Switch { width: 10; }
    ListEditor .row Button { width: 5; margin-left: 1; }
    ListEditor #add-row { width: 14; margin-top: 1; }
    """

    class Changed(Message):
        def __init__(self, items: list[Any]) -> None:
            self.items = items
            super().__init__()

    def __init__(
        self,
        items: list[Any] | None = None,
        item_type: str = "string",
        item_enum: list[Any] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.items: list[Any] = list(items) if items else []
        self.item_type = item_type
        self.item_enum = item_enum

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Vertical(id="rows")
            yield Button("+ Add item", id="add-row", variant="primary")

    def on_mount(self) -> None:
        self._rebuild()

    def _empty_value(self) -> Any:
        if self.item_enum:
            return self.item_enum[0]
        return {"string": "", "integer": 0, "number": 0.0, "boolean": False}.get(self.item_type, "")

    def _rebuild(self) -> None:
        rows = self.query_one("#rows", Vertical)
        rows.remove_children()
        for idx, item in enumerate(self.items):
            row = Horizontal(classes="row")
            rows.mount(row)
            row.mount(self._item_widget(idx, item))
            row.mount(Button("x", id=f"rm-{idx}", variant="error"))

    def _item_widget(self, idx: int, item: Any) -> Widget:
        wid = f"item-{idx}"
        if self.item_enum:
            options = [(str(o), o) for o in self.item_enum]
            return Select(
                options=options,
                value=item if item in self.item_enum else Select.NULL,
                id=wid,
            )
        if self.item_type == "boolean":
            return Switch(value=bool(item), id=wid)
        if self.item_type in ("integer", "number"):
            return Input(value=str(item), type="number", id=wid)
        return Input(value="" if item is None else str(item), id=wid)

    # --- Event handlers — keep events scoped to this widget ----------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "add-row":
            self.items.append(self._empty_value())
            self._rebuild()
            self.post_message(self.Changed(list(self.items)))
            event.stop()
        elif bid.startswith("rm-"):
            try:
                idx = int(bid[3:])
            except ValueError:
                return
            if 0 <= idx < len(self.items):
                del self.items[idx]
                self._rebuild()
                self.post_message(self.Changed(list(self.items)))
                event.stop()

    def on_input_changed(self, event: Input.Changed) -> None:
        idx = self._idx_for(event.input.id)
        if idx is None:
            return
        raw = event.value
        if self.item_type == "integer":
            try:
                self.items[idx] = int(raw)
            except ValueError:
                return
        elif self.item_type == "number":
            try:
                self.items[idx] = float(raw)
            except ValueError:
                return
        else:
            self.items[idx] = raw
        self.post_message(self.Changed(list(self.items)))
        event.stop()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        idx = self._idx_for(event.switch.id)
        if idx is None:
            return
        self.items[idx] = bool(event.value)
        self.post_message(self.Changed(list(self.items)))
        event.stop()

    def on_select_changed(self, event: Select.Changed) -> None:
        idx = self._idx_for(event.select.id)
        if idx is None or event.value is Select.NULL:
            return
        self.items[idx] = event.value
        self.post_message(self.Changed(list(self.items)))
        event.stop()

    def _idx_for(self, wid: str | None) -> int | None:
        if not wid or not wid.startswith("item-"):
            return None
        try:
            return int(wid[5:])
        except ValueError:
            return None
