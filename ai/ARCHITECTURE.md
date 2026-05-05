# ARCHITECTURE.md

Authoritative package layout and subsystem boundaries.

## Package layout

```
src/chronos/
  __init__.py
  __main__.py
  version.py
  authorization.py
  bootstrap.py
  cli.py
  config.py
  credentials.py
  domain.py
  fixture_flow.py
  ical_parser.py
  ingest.py
  index_store.py
  locking.py
  mcp_server.py
  mutations.py
  oauth.py
  paths.py
  protocols.py
  recurrence.py
  services.py
  storage.py
  storage_indexing.py
  sync.py
  caldav/
    __init__.py
    errors.py
    xml.py
    protocol.py
    session.py
  http/
    __init__.py
    auth.py
    client.py
    errors.py
  tui/
    app.py
    bindings.py
    views.py
    screens/
      agenda_screen.py
      confirm_screen.py
      day_view_screen.py
      event_detail_screen.py
      event_edit_screen.py
      grid_view_screen.py
      help_screen.py
      main_screen.py
      search_dialog_screen.py
      sync_confirm_screen.py
      sync_progress_screen.py
    widgets/
      calendar_panel.py
      date_picker.py
      event_list.py
      event_view.py
      timeline_grid.py
```

## Subsystems

- Domain/configuration:
  - `domain.py`, `protocols.py`, `config.py`, `paths.py`
- Local persistence:
  - `storage.py`, `index_store.py`, `storage_indexing.py`, `locking.py`
- Calendar semantics:
  - `ical_parser.py`, `recurrence.py`, `mutations.py`, `ingest.py`
- Sync and remote access:
  - `authorization.py`, `credentials.py`, `oauth.py`, `caldav/`, `http/`, `sync.py`
- User-facing surfaces:
  - `cli.py`, `tui/`
- MCP transport and tools:
  - `mcp_server.py`

## Notes that matter

- Raw `.ics` bytes in the mirror are authoritative; SQLite is derived state and sync control-plane state.
- `sync.py` is the inbound reconciliation engine.
- CLI and TUI are the main local mutation surfaces.
- MCP is non-destructive, but it is not read-only: additive `.ics` import is part of the current design.
- The active TUI shape is agenda/day/grid, not the older separate week/month/todo-screen model.
