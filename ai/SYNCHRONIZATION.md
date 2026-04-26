# SYNCHRONIZATION.md

The CalDAV sync algorithm for chronos. This is the load-bearing specification: any code change that affects sync must update this document first, or in the same change.

## 1. Guiding principles

1. **State, not history.** We reconcile current states on both sides; we do not replay event logs.
2. **Idempotency.** Every operation can be safely retried. Interrupted syncs resume without double-applying changes.
3. **Non-destructive by default.** When in doubt, keep data. Destructive paths require confirmation.
4. **Explicit conflict surface.** Conflicts are enumerated in §10 with defined resolutions.
5. **UID is stable, href is ephemeral.** Identity is keyed on UID + RECURRENCE-ID; href may be rewritten by the server.
6. **`href IS NULL` is the sole local-mutation signal.** Load-bearing. Meaning: "expected on the server in this calendar, not yet confirmed."

## 2. Resource identity

- **`UID`** (RFC 5545 property) is stable across copies and moves. Within `(account, calendar)`, `(UID, RECURRENCE-ID)` is the primary key.
- **`href`** is the WebDAV URL at which the resource currently lives. Ephemeral: servers may rewrite it on MOVE, on calendar renames, or across UIDVALIDITY-like epoch boundaries.
- **`etag`** is the server's per-resource version tag. Changes on any server-side write.
- Master and overrides share `UID`. Overrides are distinguished by their `RECURRENCE-ID`.
- **Duplicate UID** within a calendar → the second ingested resource gets a synthetic UID (see §C-7).
- **Missing UID** (spec violation, but defensively handled) → deterministic synthetic UID from `SHA-256(account || calendar || href || DTSTAMP || SUMMARY)` (see §C-8).

## 3. Schema

Columns listed below are the load-bearing ones; additional columns are allowed but must be documented in `index_store.py`.

```
calendar_sync_state(
  account_name    TEXT NOT NULL,
  calendar_name   TEXT NOT NULL,
  ctag            TEXT,
  sync_token      TEXT,
  synced_at       TIMESTAMP,
  PRIMARY KEY (account_name, calendar_name)
)

components(
  id              INTEGER PRIMARY KEY,
  account_name    TEXT NOT NULL,
  calendar_name   TEXT NOT NULL,
  uid             TEXT NOT NULL,
  recurrence_id   TEXT,                     -- NULL for master; set for overrides
  component_kind  TEXT NOT NULL,            -- 'VEVENT' | 'VTODO'
  href            TEXT,                     -- NULL => local-pending (see §1, AGENTS §4)
  etag            TEXT,
  raw_ics         BLOB NOT NULL,            -- authoritative bytes, copied from mirror
  summary         TEXT,
  dtstart         TIMESTAMP,
  dtend           TIMESTAMP,
  due             TIMESTAMP,                -- VTODO only
  status          TEXT,
  local_flags     TEXT,                     -- JSON
  server_flags    TEXT,                     -- JSON
  local_status    TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'trashed'
  trashed_at      TIMESTAMP,
  synced_at       TIMESTAMP
)

occurrences(
  component_id       INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
  occurrence_start   TIMESTAMP NOT NULL,
  occurrence_end     TIMESTAMP,
  is_override        INTEGER NOT NULL DEFAULT 0
)
```

Unique index:
```
CREATE UNIQUE INDEX ux_components_identity
  ON components(account_name, calendar_name, uid, COALESCE(recurrence_id, ''))
  WHERE href IS NOT NULL;
```

The `WHERE href IS NOT NULL` clause lets local-pending rows coexist with later server confirmations during the sync window.

## 4. Calendar sync scope

Per-account configuration supplies three regex lists (Python `re.fullmatch`):

- `include` — calendar display names to sync. Default: all.
- `exclude` — calendar display names to skip. Applied after `include`.
- `read_only` — server→local only. Local changes in these calendars are restored to the server state on the next sync.

## 5. Account pre-phase: calendar discovery

1. `PROPFIND` against the principal URL, collect `calendar-home-set`.
2. `PROPFIND` the home-set at depth 1 to list calendars with their display names, CTags, and supported-component-sets.
3. Apply include/exclude/read_only regexes.
4. Compare to the mirror directory tree: flag calendars present locally but missing server-side (v1 default is to warn only, not auto-create; `MKCALENDAR` is off unless config opts in).

This phase is read-only against the server unless auto-create is enabled.

## 6. CTag-gated path selection

For each in-scope calendar, fetch the current CTag via `PROPFIND` and compare to `calendar_sync_state.ctag`:

- **Fast path** — CTag unchanged. No further server I/O. Emit local-pending changes only (§7 step 5).
- **Medium path** — CTag changed *and* we have a stored `sync_token` in `calendar_sync_state`. Issue `sync-collection` REPORT (RFC 6578); apply its delta of added/changed/removed hrefs. On `valid-sync-token` error (expired token), clear the token and fall through to the slow path, then re-acquire a fresh token via `PROPFIND DAV:sync-token`. **Status: planned (Milestone 12); currently the engine always takes the slow path when CTag changes.**
- **Slow path** — CTag changed and no `sync_token` stored (first sync or after expiry). Issue a full `calendar-query` REPORT returning `(href, getetag)` pairs; compare to the local etag map. After a successful slow path, acquire the current `DAV:sync-token` via `PROPFIND` and store it for the next sync.

The fast path must stay zero-I/O beyond the CTag `PROPFIND`. Any regression that adds I/O on the fast path is a bug.

## 7. Per-calendar reconciliation (slow path, six steps)

1. **Server-side deletions.** Local hrefs not in the server's REPORT → candidate deletions. Before deleting, attempt move-detection: search for the same UID in other calendars of the same account. If found elsewhere, treat as a move; otherwise trash or delete per `local_status`.
2. **New server hrefs.** Fetch with `calendar-multiget` REPORT (batch fetch of `(getetag, calendar-data)`). Write bytes to mirror, project to index.
3. **Etag reconciliation.** For hrefs whose etag changed: re-fetch via `calendar-multiget`; merge local+server changes by `SEQUENCE` (higher wins) and tie-break on `LAST-MODIFIED` (see §10 C-3).
4. **Push local deletions.** Rows with `local_status = 'trashed'` in a writable calendar → `DELETE` with `If-Match: <etag>`. On 412, restart the step for that resource.
5. **Push local-pending rows.** Rows with `href IS NULL` and `local_status = 'active'`:
   - No prior etag known → `PUT` with `If-None-Match: *` (create).
   - Prior etag known → `PUT` with `If-Match: <etag>` (update).
   On 412 (etag mismatch): fall back to §10 C-3 three-way merge.
6. **Commit sync state.** Update `calendar_sync_state` with the new CTag and sync-token. One transaction per calendar (v1 limitation: not a single transaction across the whole reconciliation, only its commit).

## 8. Local moves (between calendars)

User moves a component from calendar A to calendar B via the TUI:

1. Copy the `.ics` bytes from A's mirror path to B's.
2. Delete the source index row.
3. Insert a target index row for B with `href = NULL`.

Next sync:
- Calendar A, step 1: detects the now-missing row; move-detect finds the matching UID in calendar B → `PushMoveOp` (WebDAV `MOVE`) if the server supports it; otherwise `PushCreateOp` to B + `PushDeleteOp` from A as a fallback pair.
- Calendar B, step 5: reconciles the `href IS NULL` row into a real resource.

## 9. Performance properties (load-bearing)

- Fast path is zero-I/O beyond the CTag `PROPFIND`.
- Hot-path queries use narrow projections (`list_pending_pushes`, `list_calendar_etag_map`, `list_uid_to_href`), never `SELECT *`.
- Account-wide UID maps (for cross-calendar move detection) are built lazily, gated by whether any step-1 candidate deletion was found.
- Server-side component counts come from CTag + sync-token comparisons, never from enumerating resources.

## 10. Conflict taxonomy

Numbered the same way pony's conflicts are, so cross-project discussions line up.

- **C-1 — Server deleted, locally modified.** The component is gone from the server's REPORT, but the local row has edits (e.g. `href IS NULL` after a local edit, or `local_flags` diverge). Resolution: re-upload via `PUT` with `If-None-Match: *`. If the server rejects with 412, treat as C-3.
- **C-2 — Locally trashed, server changed.** `local_status = 'trashed'` but etag changed on the server. Resolution: restore (flip `local_status` back to `active`) and pull server etag.
- **C-3 — Both sides changed.** Three-way merge: accept the component with the higher `SEQUENCE` number. On equal SEQUENCE, tie-break on `LAST-MODIFIED` (later wins). CATEGORIES and VALARM blocks are merged as a union regardless. Surface a notification in the TUI so the user can audit.
- **C-4 — CTag reset, sync-token invalid, or calendar URL changed.** Clear all stored etags for the calendar and full-resync, keyed on UID (+ RECURRENCE-ID). Local-pending rows (`href IS NULL`) are preserved and re-uploaded in step 5.
- **C-5 — Partial sync interrupted.** All operations are individually idempotent. On resume: re-run path selection from scratch; PUTs and DELETEs use `If-Match` to avoid clobbering; calendar_sync_state commits only after §7 step 6 succeeds, so an interrupted run leaves the old state.
- **C-6 — Mass deletion (>20% of a calendar's components gone).** Halt the reconciliation for that calendar and require explicit user confirmation before proceeding.
- **C-7 — Duplicate UID within a calendar.** The second resource gets a synthetic UID, prefixed `chronos-dup-` followed by a stable hash of the href, so the duplicate remains distinguishable across syncs.
- **C-8 — Missing UID.** Synthetic UID from `SHA-256(account || calendar || href || DTSTAMP || SUMMARY)`, prefixed `chronos-syn-`. Deterministic across syncs so we don't create duplicates.
- **C-9 — Local move pending.** `href IS NULL` in the target calendar; the source calendar still holds the real resource. Reconciled on the next sync (§8). If the source has been deleted server-side between local move and next sync, fall through to C-1.
- **C-10 — Recurring override drift.** The master's RRULE is trimmed so a server-side RECURRENCE-ID override now falls outside the new recurrence range. Keep the override as a standalone non-recurring component and surface a notification. Do not delete the override silently.
- **C-11 — VTIMEZONE changed on server.** Accept the server's VTIMEZONE. Invalidate the `occurrences` cache for any component referencing that TZID; repopulate lazily on next view.

## 11. Trash workflow

- User trashes a component → `local_status = 'trashed'`, `trashed_at = now`. The mirror file is retained.
- Next sync (writable calendar) → `DELETE` with `If-Match`. On success, purge the row and the mirror file.
- Next sync (read-only calendar) → restore: set `local_status = 'active'`, pull server etag.
- Trashed rows older than `trash_retention_days` (default 30) are purged without network action.

## 12. Garbage collection

- Accounts removed from config → their mirror trees and index rows are purged on next sync.
- Calendars that disappear from the server → their `calendar_sync_state` row is removed; index rows fall through via §7 step 1.
- Expired trash → see §11.

## 13. Known limitations (v1, accepted)

- No write-barrier analogue of IMAP's UIDPLUS: a PUT succeeded + crash before index update can leave a duplicate mirror file on next sync (self-corrects through C-1/C-3; harmless but logged).
- Per-calendar sync is committed as one transaction only at its tail — not a single transaction across the whole reconciliation.
- No exclusive write lock during sync; the TUI can enqueue new `href IS NULL` rows concurrently. SQLite's `busy_timeout` handles contention; there is no fairness guarantee.
- `sync-collection` REPORT is not universally supported (Radicale older versions, some small servers). We fall back to the slow path silently.
- Components appearing in multiple calendars via server-side links (Google's "primary + secondary" model) are treated as independent resources. Full multi-calendar label support is deferred.
