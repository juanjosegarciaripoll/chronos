# SPECIFICATIONS.md

Compact scope definition for `chronos`.

## Mission

Provide an offline-authoritative, keyboard-first calendar client that keeps its local state transparent and synchronizes with CalDAV servers without duplicating logic across CLI, TUI, and MCP surfaces.

## Existing scope

- Multi-account CalDAV synchronization.
- Local `.ics` mirror plus SQLite index and recurrence cache.
- VEVENT and VTODO support.
- Textual TUI for agenda/day/grid workflows.
- CLI for sync, inspection, mutation, import, bootstrap, and OAuth authorization.
- MCP tools for search/query/detail plus additive `.ics` import.
- OAuth browser loopback authorization where providers require it.
- Distribution through:
  - `uv tool install`
  - Windows installer releases
  - Windows portable PyInstaller bundles

## Desirable scope

- Reliable tagged-release automation.
- Better end-user packaging and installation docs.
- Runtime support for per-account mirror roots.
- TUI OAuth polish and continued UX refinement.

## Deferred or dropped scope

- Background sync daemon.
- iTIP / iMIP.
- Free/busy queries.
- VJOURNAL.
- Full `THISANDFUTURE` editing semantics.
- Keyring-backed token storage.

## Product principles

- Local files are authoritative.
- Keyboard workflows first.
- Shared domain logic across every surface.
- Packaging and release automation are part of the product, not an afterthought.
