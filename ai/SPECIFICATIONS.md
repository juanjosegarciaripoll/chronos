# SPECIFICATIONS.md

Product scope for chronos. Defines mission, v1 capabilities, and what is explicitly deferred.

## 1. Mission

A standalone open-source Python 3.13 calendar client that:

- works offline and is authoritative about its local state,
- offers a clear, keyboard-driven workflow,
- keeps storage transparent (plain `.ics` files on disk),
- synchronises with any standards-compliant CalDAV server,
- exposes the same data to humans (TUI, CLI) and to AI agents (MCP) without duplicating logic.

## 2. Four major capabilities

1. **Calendar synchronisation** — multi-account CalDAV sync with state-based reconciliation.
2. **Event and todo browsing** — day, week, month, agenda, and todo-list views in a Textual TUI.
3. **Event and todo authoring** — create, edit, move, trash, and restore from TUI or CLI.
4. **Search** — full-text (summary / description / location) plus date-range queries.

## 3. V1 scope (in scope)

Delivered or to-be-delivered in v1:

- **Sync:** CalDAV (RFC 4791), multi-account, including `sync-collection` REPORT (RFC 6578) where the server supports it, with CTag-gated fast path.
- **Storage:** per-account local mirror, vdir-style directory layout (`<mirror>/<account>/<calendar>/<uid>.ics`). Raw `.ics` bytes on disk are authoritative.
- **Index:** SQLite with FTS5 for summary/description/location, plus a rebuildable `occurrences` expansion cache.
- **Recurrence:** RRULE, RDATE, EXDATE, and RECURRENCE-ID overrides per RFC 5545.
- **Components:** VEVENT and VTODO, stored in one unified table discriminated by `component_kind`.
- **TUI:** Textual-based day / week / month / agenda / todo-list views, with screen-specific keybindings shown in the footer.
- **CLI:** `chronos sync`, `list`, `show`, `add`, `edit`, `rm`, `doctor`.
- **MCP server:** read-only tools for listing calendars, querying ranges, searching, and fetching events/todos by UID.
- **Credentials:** five backends — plaintext (for testing), environment variable, external command, encrypted keyring, and OAuth 2.0 (loopback flow, RFC 8252 + PKCE, for Google Calendar and Microsoft/Outlook).
- **Packaging:** PyInstaller standalone bundles for Linux, macOS, and Windows.

## 4. Deferred scope (v2 and later)

Not in v1. Explicitly out of scope; don't build for these without reopening the scope:

- iTIP / iMIP (RFC 5546) — meeting requests, RSVP, attendee management, SMTP send path.
- Free/busy queries.
- Browser-based UI.
- Background or periodic sync daemon.
- VJOURNAL component type.
- Multi-machine conflict arbitration beyond SEQUENCE / LAST-MODIFIED.
- Calendar sharing ACLs (RFC 3744, WebDAV ACL).
- Per-occurrence editing of very large recurrence sets (we edit via RECURRENCE-ID overrides, not splits).

## 5. UX goals

- **Keyboard-centric.** Every workflow reachable without a mouse.
- **Pane-oriented.** Calendar tree on the left, main view in the centre, detail pane on the right.
- **Screen-owned bindings.** The footer always shows the current screen's bindings, nothing else.
- **Today is cheap.** A single keystroke returns to today's view from anywhere.
- **Storage is inspectable.** A user can open any `.ics` file with any editor and the state is still coherent.

## 6. Storage and indexing

- Per-account configurable mirror path.
- One `.ics` file per calendar resource, named by UID. Raw bytes are the source of truth.
- SQLite is a searchable metadata layer and the sync control plane — never the source of truth for content.
- Unified `components` table holds both VEVENT and VTODO rows, discriminated by `component_kind`.
- `occurrences` table caches expanded recurrences in a bounded window; it is rebuildable from the `components` table at any time.
- Batched writes go through a single `connection()` context manager (see `ARCHITECTURE.md §2, Index`).
