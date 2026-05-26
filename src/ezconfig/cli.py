"""CLI entrypoint: `ezconfig <path> [<docs-url>]` or `python -m ezconfig ...`."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ezconfig import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ezconfig",
        description="TUI editor for config files (TOML, YAML, JSON, INI).",
    )
    parser.add_argument("path", type=Path, help="Path to the config file to edit.")
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help=(
            "Optional documentation URL(s). ezconfig scrapes the page(s), "
            "looks for value mentions next to each text-typed key, and turns "
            "matching fields into dropdowns with a source excerpt. Pass "
            "multiple URLs joined by '&&' (quote the whole arg in shell): "
            "'https://a.com && https://b.com'."
        ),
    )
    parser.add_argument("--version", action="version", version=f"ezconfig {__version__}")
    args = parser.parse_args(argv)

    if not args.path.exists():
        print(f"ezconfig: no such file: {args.path}", file=sys.stderr)
        return 2
    if not args.path.is_file():
        print(f"ezconfig: not a file: {args.path}", file=sys.stderr)
        return 2

    urls: list[str] = []
    if args.url is not None:
        urls = [u.strip() for u in args.url.split("&&") if u.strip()]
        for u in urls:
            if not u.startswith(("http://", "https://")):
                print(f"ezconfig: url must start with http:// or https:// — got {u!r}", file=sys.stderr)
                return 2

    # Import here so --help/--version don't pay the Textual import cost.
    from ezconfig.app import run

    run(args.path, web_urls=urls)
    return 0


if __name__ == "__main__":
    sys.exit(main())
