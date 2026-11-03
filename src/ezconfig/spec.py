"""FieldSpec: how to show and check one key."""
from __future__ import annotations

import re
from dataclasses import dataclass, fields
from typing import Any

COLOR_RE = re.compile(r"^(#|0x)(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


@dataclass
class FieldSpec:
    widget: str = ""  # switch | color | number | text | choice | list | readonly; "" = unknown
    description: str = ""
    default: Any = None
    options: list[Any] | None = None
    option_notes: list[str] | None = None  # one per option
    strict: bool = False  # only `options` are valid
    minimum: float | None = None
    maximum: float | None = None
    integer: bool = False
    item_type: str = ""  # list items: string | integer | number | boolean
    item_options: list[Any] | None = None
    deprecated: str = ""  # message when deprecated
    source: str = "value"  # value | schema (…) | guess (…) | docs (…)
    warning: str = ""  # set for sources that may be wrong

    def merge(self, other: FieldSpec) -> FieldSpec:
        """Copy of self with every field `other` sets taking precedence."""
        merged = FieldSpec()
        for f in fields(FieldSpec):
            mine, theirs = getattr(self, f.name), getattr(other, f.name)
            if f.name == "default":
                unset = theirs is None
            else:
                unset = theirs is None or theirs is False or (isinstance(theirs, str) and not theirs)
            setattr(merged, f.name, mine if unset else theirs)
        if other.source == "value":
            merged.source = self.source
        # A schema that only says "string" must not hide a color picker.
        if self.widget == "color" and other.widget == "text" and not other.options:
            merged.widget = "color"
        return merged


def infer(value: Any) -> FieldSpec:
    """FieldSpec from a value alone."""
    if isinstance(value, bool):
        return FieldSpec(widget="switch")
    if isinstance(value, int):
        return FieldSpec(widget="number", integer=True)
    if isinstance(value, float):
        return FieldSpec(widget="number")
    if isinstance(value, str):
        return FieldSpec(widget="color" if COLOR_RE.match(value) else "text")
    if isinstance(value, list):
        if any(isinstance(v, (dict, list)) for v in value):
            return FieldSpec(widget="readonly")
        return FieldSpec(widget="list", item_type=_item_type(value))
    if isinstance(value, dict):
        return FieldSpec(widget="readonly")
    return FieldSpec(widget="text")


def _item_type(values: list[Any]) -> str:
    sample = values[0] if values else ""
    if isinstance(sample, bool):
        return "boolean"
    if isinstance(sample, int):
        return "integer"
    if isinstance(sample, float):
        return "number"
    return "string"
