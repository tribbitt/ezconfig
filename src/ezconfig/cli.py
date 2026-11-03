"""Command line: `ezconfig TARGET [DOCS ...]`."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ezconfig import __version__

CONFIG_SUFFIXES = {".toml", ".yaml", ".yml", ".json", ".jsonc", ".ini", ".cfg", ".conf"}


def _candidates(folder: Path, name: str) -> tuple[list[Path], list[Path]]:
    """(likely, other) config files directly inside `folder`."""
    likely: list[Path] = []
    other: list[Path] = []
    for p in sorted(folder.iterdir()):
        if not p.is_file() or (p.name.startswith(".") and p.suffix.lower() not in CONFIG_SUFFIXES):
            continue
        stem = p.stem.lower()
        if stem in ("config", "settings", name.lower(), f"{name.lower()}rc") or p.name == "config":
            likely.append(p)
        elif p.suffix.lower() in CONFIG_SUFFIXES:
            other.append(p)
    return likely, other


def find_config(target: str) -> tuple[Path | None, list[Path]]:
    """Resolve TARGET to a file. Returns (path, []) on success, or (None,
    candidates) when it's ambiguous or nothing was found."""
    path = Path(target).expanduser()
    if path.is_file():
        return path, []
    folders: list[Path] = []
    files: list[Path] = []
    if path.is_dir():
        folders.append(path)
    elif "/" not in target and not path.suffix:
        # An app name: look in the usual places.
        home = Path.home()
        xdg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        folders += [xdg / target, home / ".config" / target, home / f".{target}"]
        if sys.platform == "darwin":
            folders.append(home / "Library" / "Application Support" / target)
        files += [home / f".{target}rc", home / f".{target}config", home / f".{target}"]
        files += [home / f".{target}{s}" for s in sorted(CONFIG_SUFFIXES)]
    else:
        return (path, []) if path.parent.is_dir() else (None, [])
    likely: list[Path] = [f for f in files if f.is_file()]
    other: list[Path] = []
    seen: set[Path] = set()
    for folder in folders:
        if folder.is_dir() and folder.resolve() not in seen:
            seen.add(folder.resolve())
            a, b = _candidates(folder, path.name)
            likely += a
            other += b
    if len(likely) == 1:
        return likely[0], []
    if not likely and len(other) == 1:
        return other[0], []
    return None, likely + other


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ezconfig",
        description="Edit a config file in the terminal.",
        epilog="Example: ezconfig ghostty https://ghostty.org/docs/config/reference",
    )
    parser.add_argument("target", help="config file, folder, or app name (looked up in ~/.config and similar)")
    parser.add_argument("docs", nargs="*", help="docs to read options from: URL, file, or man:NAME")
    parser.add_argument("--schema", help="JSON Schema to use: URL or file")
    parser.add_argument("--refresh", action="store_true", help="re-download cached schemas and docs")
    parser.add_argument("--list", action="store_true", help="print keys and options instead of opening the editor")
    parser.add_argument("--version", action="version", version=f"ezconfig {__version__}")
    args = parser.parse_args(argv)

    path, candidates = find_config(args.target)
    if path is None:
        if candidates:
            print(f"ezconfig: several config files match {args.target!r}; pass one:", file=sys.stderr)
            for c in candidates:
                print(f"  {c}", file=sys.stderr)
        else:
            print(f"ezconfig: no config file found for {args.target!r}", file=sys.stderr)
        return 2
    for source in args.docs:
        if not (source.startswith(("http://", "https://", "man:")) or Path(source).expanduser().is_file()):
            print(f"ezconfig: docs must be a URL, a file, or man:NAME; got {source!r}", file=sys.stderr)
            return 2

    from ezconfig import net
    net.refresh = args.refresh
    docs = [str(Path(s).expanduser()) if Path(s).expanduser().is_file() else s for s in args.docs]

    if args.list:
        try:
            return print_keys(path, docs, args.schema)
        except BrokenPipeError:  # output piped into e.g. `head`
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            return 1

    from ezconfig.app import run  # Textual import is slow; keep --help fast
    run(path, docs=docs, schema=args.schema)
    return 0


def print_keys(path: Path, docs: list[str], schema: str | None) -> int:
    from ezconfig import formats
    from ezconfig.index import build
    from ezconfig.widgets import option_label

    try:
        doc = formats.load(path) if path.exists() else formats.new(path)
    except Exception as exc:
        print(f"ezconfig: can't load {path}: {exc}", file=sys.stderr)
        return 1
    index = build(doc, docs, schema)
    print(f"{path} ({doc.format_name})")
    for line in index.sources:
        print(f"  {line}")
    for label, specs in (("keys", index.specs), ("unset keys", index.unset)):
        print(f"\n{label} ({len(specs)}):")
        for key_path, spec in sorted(specs.items()):
            line = f"  {'.'.join(key_path)}  [{spec.widget}]  {spec.source}"
            if spec.options:
                shown = ", ".join(option_label(o) for o in spec.options[:12])
                more = f", … ({len(spec.options)})" if len(spec.options) > 12 else ""
                line += f"\n      options: {shown}{more}"
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
