# ARCHITECTURE.md

Authoritative layout and subsystem boundaries for chronos. When a module's responsibilities move, update this file in the same change.

## 1. Package layout

```
src/chronos/
  __init__.py
  __main__.py                # python -m chronos entrypoint
  version.py                 # __version__ (release workflow owns this)
  cli.py                     # argparse command dispatch
  config.py                  # TOML -> domain objects
  domain.py                  # Frozen dataclasses (AccountConfig, CalendarConfig,
                             #   CalendarRef, ComponentRef, VEvent, VTodo,
                             #   Occurrence, ...)
  protocols.py               # Repository/service Protocol interfaces
  paths.py                   # XDG + Windows dir resolution, bundled_docs_path()
  storage.py                 # vdir-style .ics mirror repository
  index_store.py             # SQLite index: components, occurrences,
                             #   calendar_sync_state, FTS5
  storage_indexing.py        # Mirror -> index projection pipeline
  ical_parser.py             # RFC 5545 parse/serialize; thin wrapper over `icalendar`
  recurrence.py              # RRULE/RDATE/EXDATE expansion; override application
  caldav_client.py           # CalDAV session: PROPFIND, REPORT (sync-collection,
                             #   calendar-query, calendar-multiget), GET/PUT/DELETE
  sync.py                    # Two-phase CalDAV sync engine (plan / execute)
  credentials.py             # Plaintext / env / command / encrypted backends
  services.py                # Doctor diagnostics, mirror integrity scan
  mutations.py               # Shared write helpers: build_event_ics,
                             #   generate_uid, trashed_copy. Used by CLI + TUI.
  fixture_flow.py            # Deterministic dev ingest (for local testing)
  ingest.py                  # ICS file ingestion: parse + mirror-write + index-upsert
                             #   with href=NULL. Entry point for CLI + MCP import.
  mcp_state.py               # MCP state file: port + auth token for the running
                             #   TCP server; read/write/remove helpers.
  mcp_transport.py           # TCP server, stdio bridge, stdio self-contained mode.
                             #   serve_tcp / run_stdio_bridge / run_stdio_standalone.
  mcp_server.py              # FastMCP tools: list_calendars, query_range, search,
                             #   get_event, get_todo, import_ics.
                             #   run_mcp_stdio (chronos mcp entry point),
                             #   start_tcp_server (TUI / daemon).
  tui/
    app.py                   # ChronosApp + TuiServices dependency bundle
    bindings.py              # Per-screen binding builders + key constants
    views.py                 # Pure projection helpers: window math,
                             #   gather_occurrences, search_components,
                             #   render_event_detail. No Textual imports.
    screens/
      main_screen.py         # Three-pane layout; owns view-switch + global bindings
      day_view_screen.py     # title_for / window_for / rows_for helpers
      week_view_screen.py    # idem
      month_view_screen.py   # idem
      agenda_screen.py       # idem
      todo_list_screen.py    # title_for / rows_for
      event_detail_screen.py # Read-only modal with [back] [edit]
      event_edit_screen.py   # Form for create/edit; emits EditDraft
      sync_confirm_screen.py # Pre-sync confirmation, on_confirm callback
      search_dialog_screen.py# Live in-memory search over loaded components
      confirm_screen.py      # Generic [y]/[n] modal
    widgets/
      calendar_panel.py      # Collapsible per-account calendar tree
      event_list.py          # DataTable of events/todos, keyed by ref+instance
      event_view.py          # Read-only event/todo detail (Static)
      date_picker.py         # Input + parse_date_input helper
```

All modules above are described in `TASKS.md` milestones; don't treat their existence as a given before the corresponding milestone is complete.

## 2. Subsystems

**Domain layer** (`domain.py`, `protocols.py`). Frozen `@dataclass` types for configuration and content; `Protocol` classes for every repository and service. Domain types are framework-agnostic — no Textual, SQLite, or CalDAV imports leak in here.

**Configuration** (`config.py`, `paths.py`). A single TOML file is parsed directly into domain objects, with no intermediate DTO layer. Path values support `~`, `$VAR`, and `%VAR%` expansion. `paths.py` handles XDG on Linux/macOS and the Windows equivalents, plus `bundled_docs_path()` to detect PyInstaller-frozen execution.

**Storage mirror** (`storage.py`). One `.ics` file per calendar resource, laid out as `<mirror>/<account>/<calendar>/<uid>.ics` (vdirsyncer-compatible). Raw bytes are the authoritative content; the index is derived. The `MirrorRepository` protocol exposes write-new, overwrite, move, and delete operations that are crash-safe (temp-file + rename).

**Index** (`index_store.py`, `storage_indexing.py`). SQLite with:
- `components` — unified table for VEVENT + VTODO, discriminated by `component_kind`.
- `occurrences` — expansion cache (see `RECURRENCE.md`).
- `calendar_sync_state` — CTag, sync-token, etag bookkeeping per calendar.
- FTS5 virtual table over `components.summary`, `description`, `location`.

All writes go through a single `connection()` context manager that batches within a transaction. Readers use short connections; no long-lived cursors.

**Recurrence** (`recurrence.py`). RRULE / RDATE / EXDATE expansion within a bounded window; RECURRENCE-ID overrides are applied as concrete rows that replace the expanded occurrence at the matching instant. The `occurrences` cache is invalidated on any write to a master or override. Details live in `RECURRENCE.md`.

**Sync** (`sync.py`, `caldav_client.py`). Two-phase (plan, then execute). CTag-gated path selection: fast path (zero-I/O beyond PROPFIND), medium path (sync-collection REPORT), slow path (full calendar-query REPORT + etag compare). `href IS NULL` is the only local-mutation signal. Conflict taxonomy lives in `SYNCHRONIZATION.md §10`.

**TUI** (`tui/`). Textual. `ChronosApp` is a thin host; everything lives in `MainScreen` (three panes: calendar tree, view list, detail). Each screen owns its `BINDINGS` list; the footer shows only the current screen's bindings. Screens use the public Textual API exclusively — `self.app.push_screen`, `self.app.notify`. Screens never call `self.app._private_method`. The hard logic — window arithmetic, occurrence joining, search, detail rendering — lives in `tui/views.py` as pure functions, and is unit-tested without a Textual app. Mutating actions go through `ConfirmScreen` (trash) or `EventEditScreen` (create/edit), both of which take callbacks rather than references to the app.

**CLI** (`cli.py`). `argparse` dispatch to subcommands. Same repositories as the TUI; nothing TUI-specific leaks in. Write helpers shared with the TUI (`build_event_ics`, `generate_uid`, `trashed_copy`) live in `mutations.py`.

**Ingestion** (`ingest.py`). Parses an external `.ics` payload, splits it into per-UID groups, and writes each group to the mirror + index with `href=NULL`. The `href IS NULL` signal causes the next `chronos sync` to push the imported component to the server. Used by `cli.cmd_import` and the MCP `import_ics` tool. Additive only — no delete path.

**MCP** (`mcp_server.py`, `mcp_transport.py`, `mcp_state.py`). Three-module split:
- `mcp_server.py` — FastMCP tools (five read + `import_ics`). `run_mcp_stdio` is the `chronos mcp` entry point; `start_tcp_server` is for the TUI / daemon. No destructive tools.
- `mcp_transport.py` — asyncio TCP server (`serve_tcp`), transparent stdio↔TCP bridge (`run_stdio_bridge`), and self-contained stdio mode (`run_stdio_standalone`). `chronos mcp` auto-selects: bridge if a running instance is detected, self-contained otherwise.
- `mcp_state.py` — state file (`user_data_dir() / "mcp_server.json"`) carrying the TCP port and auth token. Written by `start_tcp_server`, read by `run_mcp_stdio`.

## 3. Data flow

```
  config.toml ── config.py ──► AccountConfig(s)
                                   │
                                   ▼
    caldav_client.py ◄──── sync.py ────► storage.py (mirror .ics files)
                                   │                 │
                                   ▼                 ▼
                         index_store.py (components, occurrences, sync_state)
                                   │
                  ┌────────────────┼────────────────┐
                  ▼                ▼                ▼
                tui/             cli.py         mcp_server.py
```

`sync.py` is the only writer on the inbound (server → local) path. The TUI and CLI are the only writers on the outbound (user → local) path. The MCP server reads only.

## 4. Dependencies

**Runtime:** `caldav`, `icalendar`, `python-dateutil`, `textual`, `mcp`. (`httpx` may appear as a transitive dep via `caldav`; not used directly unless `caldav` proves insufficient.)

**Dev:** `ruff`, `mypy`, `basedpyright`, `pytest`, `pytest-asyncio`, `pyinstaller`, `mkdocs-material`.

The authoritative approved-deps list lives in `CONVENTIONS.md §7`. If the list here and there disagree, `CONVENTIONS.md` wins.
