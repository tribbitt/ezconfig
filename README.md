# ezconfig

Edit config files in the terminal. Each key gets a switch, color picker, option list, number input, or list editor.

## Install

Requires Python 3.11 or later.

```sh
pipx install git+https://github.com/tribbitt/ezconfig
```

With uv: `uv tool install git+https://github.com/tribbitt/ezconfig`. To update, run `pipx upgrade ezconfig` or `uv tool upgrade ezconfig`.

## Use

```sh
ezconfig TARGET [DOCS ...]
```

- `TARGET`: a config file, a folder, or an app name. `ezconfig ghostty` opens `~/.config/ghostty/config`. A missing file is created on save.
- `DOCS`: where to read options from: a URL, a file, or `man:NAME`.

```sh
ezconfig ~/.config/micro/settings.json
ezconfig ghostty https://ghostty.org/docs/config/reference
ezconfig ~/.ssh/config man:ssh_config
```

| Flag | Effect |
|---|---|
| `--schema URL\|FILE` | use this JSON Schema |
| `--list` | print keys and options instead of opening the editor |
| `--refresh` | download cached schemas and docs again |

## Keys

| Key | Action |
|---|---|
| Enter | edit the selected key |
| Esc | back to the tree |
| Ctrl+F | find a key |
| Ctrl+T | show or hide unset keys |
| Delete | remove the selected key |
| Ctrl+O | review changes and schema problems |
| Ctrl+S | save |
| Ctrl+R | reload from disk |
| Ctrl+Q | quit |

## Options

Options come from, in order:

1. A JSON Schema: `--schema`, a `$schema` key, a `# yaml-language-server: $schema=` or `#:schema` comment, or the [SchemaStore](https://www.schemastore.org) catalog.
2. Docs.
3. The key name: fonts, timezones, locales, shells, editors, pagers, theme files next to the config, log levels.

Unset keys are keys the schema or docs list but the file doesn't have. Giving one a value adds it to the file.

## Formats

TOML, YAML, JSON, and INI keep comments and layout. Other files use the text format: values are found by pattern, and only edited values change.

## Files

- Backup: the first save copies the original to `~/Downloads/<app> config/`. `info.txt` there logs each save.
- Cache: schemas, docs, and the font list are kept in `~/.cache/ezconfig` for a day.

## Test

```sh
git clone https://github.com/tribbitt/ezconfig
cd ezconfig
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

## License

MIT
