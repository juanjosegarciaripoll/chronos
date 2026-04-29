# CONVENTIONS.md

Engineering and release rules for `chronos`.

## Runtime and tooling

- Python `>=3.13`
- `uv` for environment and command execution
- `hatchling` as the build backend

## Quality gates

Every substantive change is expected to pass:

```powershell
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/
uv run basedpyright src/
uv run python -m pytest --cov=chronos --cov-branch --cov-fail-under=85 tests/
```

## Testing rules

- Use `unittest` test classes, executed via `pytest`.
- Use real `SqliteIndexRepository` and real mirror files in temp directories.
- Use `FakeCalDAVSession` for sync logic; default tests do not hit the network.
- Treat coverage as a gate.

## Approved runtime dependencies

- `caldav`
- `icalendar`
- `python-dateutil`
- `textual`
- `mcp`
- `tomli-w`

`anyio` is available transitively through `mcp` and may be used in MCP transport code.

## Versioning and changelog

- The canonical release version lives in:
  - `pyproject.toml`
  - `src/chronos/version.py`
- `CHANGELOG.md` keeps the release headings.
- Normal development does not hand-edit release versions.
- Tagged release automation updates the version files and stamps the matching changelog heading.

## Packaging targets

The supported distribution paths are:

1. `uv tool install`
2. Windows installer `.exe`
3. Windows portable PyInstaller bundle

## Release process

- Trigger: git tag matching `v*`
- Entry point: `scripts/build.py`
- Required actions:
  - derive the version from the tag
  - run checks and tests
  - update version files
  - stamp `CHANGELOG.md`
  - build release artifacts
  - publish the GitHub release

## Product constraints

- No destructive MCP tools.
- No browser-based application UI track.
- Keep docs synchronized with code.
