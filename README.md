# chronos

Terminal-first calendar client with CalDAV sync, a local `.ics` mirror, a SQLite index, a Textual TUI, and an MCP surface for AI clients.

## Current features

- Multi-account CalDAV sync with CTag fast path, `sync-collection` medium path, and full slow-path reconciliation.
- Local mirror of calendar resources as plain `.ics` files plus a searchable SQLite index with recurrence cache.
- CLI for sync, inspection, account bootstrap, OAuth authorization, local event editing, reset, and `.ics` import.
- Textual TUI for agenda, day, and multi-day grid views, with create/edit/trash/search/sync flows.
- MCP server with stdio mode, TCP bridge mode, read tools, and additive `import_ics`.
- OAuth loopback flow for providers that require it.

## Google OAuth on a headless VM

For Google accounts, use an OAuth client of type "Desktop app". If chronos
needs tokens while running in a headless SSH terminal, it prints an
authorization URL instead of trying to open a browser on the VM. Open that URL
on your local machine, finish Google sign-in, then copy the final
`http://127.0.0.1:.../?code=...&state=...` URL from the browser address bar
back into chronos. A browser connection error on that final page is expected:
`127.0.0.1` is local to the browser machine, not the VM.

You can force this mode explicitly:

```sh
CHRONOS_OAUTH_FLOW=remote-browser chronos sync
chronos oauth authorize --account google --remote-browser
```

On a local graphical desktop, chronos still uses the browser loopback flow and
captures the redirect automatically. Terminal browsers such as `w3m`, `lynx`,
and `links` are treated as headless mode, not as usable graphical browsers.

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
