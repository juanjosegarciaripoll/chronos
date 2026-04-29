# PLAN.md

Current project plan and scope summary. This replaces the milestone backlog previously kept in `TASKS.md`.

## Existing features

- CalDAV sync for multiple accounts.
- Three sync paths: fast (`CTag` unchanged), medium (`sync-collection`), slow (`calendar-query` + multiget).
- Crash-safe local persistence: atomic mirror writes, transactional SQLite updates, resumable sync, sync lockfile.
- Local calendar mirror as plain `.ics` files plus SQLite metadata, FTS search, and recurrence cache.
- VEVENT and VTODO support, including recurrence overrides.
- CLI commands for:
  - bootstrap and config editing
  - account add/list/remove
  - sync, reset, doctor
  - list/show/add/edit/rm
  - OAuth authorize
  - `.ics` import
- Textual TUI with:
  - agenda, day, and grid views
  - calendar selection panel
  - create, edit, trash, search, and sync flows
  - modal event detail and sync progress
- MCP support with:
  - stdio self-contained mode
  - stdio-to-TCP bridge when a running app instance exists
  - read/query tools plus additive `import_ics`
- OAuth loopback flow for providers that require browser authorization.

## Desirable next features

### Release and packaging

- Support the three distribution paths explicitly:
  - `uv tool install`
  - Windows self-installing release package
  - Windows relocatable PyInstaller bundle
- Maintain one release entry point: `scripts/build.py`.
- Run release automation on tags matching `v*`.
- Release flow must:
  - derive the version from the tag
  - run quality gates and tests
  - update `pyproject.toml` and `src/chronos/version.py`
  - stamp a matching heading in `CHANGELOG.md`
  - build all release artifacts
  - create and publish the GitHub release

### Product/documentation cleanup

- Keep internal docs aligned with the actual code, especially TUI shape, MCP behavior, and packaging.
- Add end-user installation and usage documentation beyond the internal `ai/` docs.

### Runtime polish

- Honor per-account `mirror_path` at runtime instead of always using the shared default mirror root.
- Improve TUI OAuth UX so first-run authorization does not require leaving the TUI.
- Keep refining sync UX and progress reporting in the TUI.
- Strengthen release verification for packaged binaries.

## Lower-priority deferred features

These are not current priorities and should not drive design now.

- Background sync daemon.
- iTIP / iMIP workflows.
- Free/busy support.
- VJOURNAL support.
- Multi-calendar link semantics beyond current resource-level handling.
- Full `THISANDFUTURE` recurrence editing semantics.
- Keyring-backed OAuth token storage.
- Advanced multi-machine conflict arbitration beyond the current CalDAV-driven model.

## Scope notes

- MCP may add data, but must not expose destructive tools.
- Browser-based product work is dropped for now; OAuth browser flow remains in scope because providers require it.
- Release engineering is now first-class work, not a future placeholder.
