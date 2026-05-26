"""Generic key-name heuristics for enriching FieldSpecs without per-app rules.

Two layers:

1. PROVIDER_PATTERNS: regex on the leaf key name → system option provider
   (fonts, timezones, locales, shells). Confidence is moderate — the values
   are real (installed on the system), but the app may not accept every entry
   (e.g. a font listed by fc-list may not be supported by your specific app).

2. GUESS_ENUMS: regex on the leaf key name → a known-common enum set
   (light/dark/auto, log levels). Confidence is low — these are conventional
   but not universal, so the TUI shows a warning.

All matches are by leaf key name only and case-insensitive. Schema-derived
enums always take precedence; heuristics only fire when the schema is silent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from ezconfig import providers


@dataclass
class HeuristicHit:
    options: list[Any]
    source: str
    warning: str


PROVIDER_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(font|font[_-]?family|fontfamily)$", re.IGNORECASE), "fonts"),
    (re.compile(r"^(timezone|tz|time[_-]?zone)$", re.IGNORECASE), "timezones"),
    (re.compile(r"^(locale|lang|language)$", re.IGNORECASE), "locales"),
    (re.compile(r"^(shell|login[_-]?shell)$", re.IGNORECASE), "shells"),
]

# (key-name regex, options list, warning text)
GUESS_ENUMS: list[tuple[re.Pattern[str], list[Any], str]] = [
    (
        re.compile(
            r"^(theme[_-]?mode|appearance|color[_-]?scheme[_-]?mode|colormode|mode)$",
            re.IGNORECASE,
        ),
        ["light", "dark", "auto", "system"],
        "Guessed from key name — app may use different values.",
    ),
    (
        re.compile(r"^(log[_-]?level|loglevel|verbosity|level)$", re.IGNORECASE),
        ["trace", "debug", "info", "warn", "warning", "error", "fatal", "critical"],
        "Guessed from key name — app may use different names.",
    ),
]


def lookup(leaf_key: str, current_value: Any) -> HeuristicHit | None:
    """Find heuristic options for a leaf key. Returns None when no rule matches
    or when a matching provider returned an empty list (e.g. fc-list missing)."""
    for pattern, provider_name in PROVIDER_PATTERNS:
        if pattern.match(leaf_key):
            fn: Callable[[], list[str]] = providers.PROVIDERS.get(provider_name)
            if fn is None:
                continue
            options = fn()
            if not options:
                return None
            # If the user's current value isn't in the list, keep it as a valid
            # selection by prepending — better than forcing them to retype it.
            if isinstance(current_value, str) and current_value and current_value not in options:
                options = [current_value] + options
            return HeuristicHit(
                options=options,
                source=f"heuristic ({provider_name})",
                warning=f"From your system ({provider_name}) — app may not accept every entry.",
            )
    for pattern, options, warning in GUESS_ENUMS:
        if pattern.match(leaf_key):
            opts = list(options)
            if isinstance(current_value, str) and current_value and current_value not in opts:
                opts = [current_value] + opts
            return HeuristicHit(
                options=opts,
                source="heuristic (guessed)",
                warning=warning,
            )
    return None
