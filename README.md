# chronos

Terminal-first calendar client with CalDAV sync, a local `.ics` mirror, a SQLite index, a Textual TUI, and an MCP surface for AI clients.

## Current features

- Multi-account CalDAV sync with CTag fast path, `sync-collection` medium path, and full slow-path reconciliation.
- Local mirror of calendar resources as plain `.ics` files plus a searchable SQLite index with recurrence cache.
- CLI for sync, inspection, account bootstrap, OAuth authorization, local event editing, reset, and `.ics` import.
- Textual TUI for agenda, day, and multi-day grid views, with create/edit/trash/search/sync flows.
- MCP server with stdio mode, TCP bridge mode, read tools, and additive `import_ics`.
- OAuth loopback flow for providers that require it.

## Installation paths

1. `uv tool install .`
2. Windows installer built at release time.
3. Windows portable PyInstaller bundle built at release time.

For development:

```powershell
uv sync --group dev --group build
uv run python -m pytest
```

## Release outputs

Tagged releases `v*` are intended to publish:

- source distribution
- Windows installer `.exe`
- Windows portable PyInstaller archive

## Project docs

- [ai/PLAN.md](ai/PLAN.md)
- [ai/SPECIFICATIONS.md](ai/SPECIFICATIONS.md)
- [ai/ARCHITECTURE.md](ai/ARCHITECTURE.md)
- [ai/CONVENTIONS.md](ai/CONVENTIONS.md)
