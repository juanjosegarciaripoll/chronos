from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from chronos.domain import (
    CalendarRef,
    ComponentKind,
    ComponentRef,
    LocalStatus,
    Occurrence,
    StoredComponent,
    SyncState,
    VEvent,
    VTodo,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar_sync_state(
    account_name   TEXT NOT NULL,
    calendar_name  TEXT NOT NULL,
    ctag           TEXT,
    sync_token     TEXT,
    synced_at      TEXT,
    PRIMARY KEY (account_name, calendar_name)
);

CREATE TABLE IF NOT EXISTS components(
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name    TEXT NOT NULL,
    calendar_name   TEXT NOT NULL,
    uid             TEXT NOT NULL,
    recurrence_id   TEXT,
    component_kind  TEXT NOT NULL,
    href            TEXT,
    etag            TEXT,
    raw_ics         BLOB NOT NULL,
    summary         TEXT,
    description     TEXT,
    location        TEXT,
    dtstart         TEXT,
    dtend           TEXT,
    due             TEXT,
    status          TEXT,
    local_flags     TEXT NOT NULL DEFAULT '[]',
    server_flags    TEXT NOT NULL DEFAULT '[]',
    local_status    TEXT NOT NULL DEFAULT 'active',
    trashed_at      TEXT,
    synced_at       TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_components_identity
    ON components(account_name, calendar_name, uid, COALESCE(recurrence_id, ''))
    WHERE href IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_components_calendar
    ON components(account_name, calendar_name);

CREATE INDEX IF NOT EXISTS ix_components_pending
    ON components(account_name, calendar_name)
    WHERE href IS NULL AND local_status = 'active';

CREATE TABLE IF NOT EXISTS occurrences(
    component_id      INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
    occurrence_start  TEXT NOT NULL,
    occurrence_end    TEXT,
    is_override       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_occurrences_component
    ON occurrences(component_id);

CREATE INDEX IF NOT EXISTS ix_occurrences_range
    ON occurrences(occurrence_start, occurrence_end);

CREATE VIRTUAL TABLE IF NOT EXISTS components_fts USING fts5(
    summary, description, location,
    content='components', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS components_fts_insert
AFTER INSERT ON components BEGIN
    INSERT INTO components_fts(rowid, summary, description, location)
    VALUES (new.id, new.summary, new.description, new.location);
END;

CREATE TRIGGER IF NOT EXISTS components_fts_delete
AFTER DELETE ON components BEGIN
    INSERT INTO components_fts(components_fts, rowid, summary, description, location)
    VALUES ('delete', old.id, old.summary, old.description, old.location);
END;

CREATE TRIGGER IF NOT EXISTS components_fts_update
AFTER UPDATE ON components BEGIN
    INSERT INTO components_fts(components_fts, rowid, summary, description, location)
    VALUES ('delete', old.id, old.summary, old.description, old.location);
    INSERT INTO components_fts(rowid, summary, description, location)
    VALUES (new.id, new.summary, new.description, new.location);
END;
"""

_COMPONENT_COLUMNS = (
    "account_name",
    "calendar_name",
    "uid",
    "recurrence_id",
    "component_kind",
    "href",
    "etag",
    "raw_ics",
    "summary",
    "description",
    "location",
    "dtstart",
    "dtend",
    "due",
    "status",
    "local_flags",
    "server_flags",
    "local_status",
    "trashed_at",
    "synced_at",
)


class SqliteIndexRepository:
    """SQLite-backed projection of every component in the local mirror.

    Connections are per-thread. The TUI runs sync on a worker thread
    while the UI thread keeps reading from the index for view
    refreshes; sqlite3's default `check_same_thread=True` rejects
    cross-thread access on a shared connection. WAL mode plus
    `busy_timeout` lets multiple connections (one per thread) talk to
    the same file safely. We open `check_same_thread=False` so that
    `close()` — which the TUI calls on app exit, from a thread that
    didn't necessarily open every cached connection — doesn't itself
    trip the same guard.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Mirror of every connection we hand out, so `close()` can
        # walk them. Guarded by `_connections_lock` because the
        # registration happens on whatever thread first asks for a
        # connection.
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        # Open + run the schema once on the constructing thread; the
        # `CREATE … IF NOT EXISTS` statements are idempotent, so
        # connections opened later don't need to re-run them.
        self._open_connection().executescript(_SCHEMA)

    def _open_connection(self) -> sqlite3.Connection:
        existing = cast(sqlite3.Connection | None, getattr(self._local, "conn", None))
        if existing is not None:
            return existing
        conn = sqlite3.connect(
            self._path, isolation_level=None, check_same_thread=False
        )
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        self._local.conn = conn
        with self._connections_lock:
            self._connections.append(conn)
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._open_connection()

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection]:
        conn = self._open_connection()
        if conn.in_transaction:
            yield conn
            return
        conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def close(self) -> None:
        with self._connections_lock:
            for conn in self._connections:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()
            self._connections.clear()
        # Drop the thread-local cache too so a subsequent call on
        # this repository (uncommon but possible in tests) reopens
        # fresh connections instead of returning closed handles.
        self._local = threading.local()

    def upsert_component(self, component: StoredComponent) -> None:
        row = _component_to_row(component)
        with self.connection() as conn:
            existing_id = _find_component_id(conn, component.ref)
            if existing_id is None:
                placeholders = ", ".join("?" for _ in _COMPONENT_COLUMNS)
                columns = ", ".join(_COMPONENT_COLUMNS)
                conn.execute(
                    f"INSERT INTO components ({columns}) VALUES ({placeholders})",
                    tuple(row[c] for c in _COMPONENT_COLUMNS),
                )
            else:
                assignments = ", ".join(f"{c} = ?" for c in _COMPONENT_COLUMNS)
                conn.execute(
                    f"UPDATE components SET {assignments} WHERE id = ?",
                    (*(row[c] for c in _COMPONENT_COLUMNS), existing_id),
                )
            _invalidate_master_occurrences(conn, component.ref)

    def get_component(self, ref: ComponentRef) -> StoredComponent | None:
        with self.connection() as conn:
            cursor = conn.execute(
                f"SELECT {', '.join(_COMPONENT_COLUMNS)} FROM components "
                "WHERE account_name = ? AND calendar_name = ? AND uid = ? "
                "AND COALESCE(recurrence_id, '') = COALESCE(?, '')",
                (
                    ref.account_name,
                    ref.calendar_name,
                    ref.uid,
                    ref.recurrence_id,
                ),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_component(row)

    def delete_component(self, ref: ComponentRef) -> None:
        with self.connection() as conn:
            conn.execute(
                "DELETE FROM components "
                "WHERE account_name = ? AND calendar_name = ? AND uid = ? "
                "AND COALESCE(recurrence_id, '') = COALESCE(?, '')",
                (
                    ref.account_name,
                    ref.calendar_name,
                    ref.uid,
                    ref.recurrence_id,
                ),
            )
            _invalidate_master_occurrences(conn, ref)

    def set_occurrences(
        self, ref: ComponentRef, occurrences: Sequence[Occurrence]
    ) -> None:
        with self.connection() as conn:
            component_id = _find_component_id(conn, ref)
            if component_id is None:
                return
            conn.execute(
                "DELETE FROM occurrences WHERE component_id = ?",
                (component_id,),
            )
            for occ in occurrences:
                conn.execute(
                    "INSERT INTO occurrences "
                    "(component_id, occurrence_start, occurrence_end, is_override) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        component_id,
                        _datetime_to_sql(occ.start),
                        _datetime_to_sql(occ.end),
                        1 if occ.is_override else 0,
                    ),
                )

    def query_occurrences(
        self,
        calendar: CalendarRef,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[Occurrence, ...]:
        start_sql = _datetime_to_sql(window_start)
        end_sql = _datetime_to_sql(window_end)
        with self.connection() as conn:
            cursor = conn.execute(
                "SELECT c.account_name, c.calendar_name, c.uid, c.recurrence_id, "
                "o.occurrence_start, o.occurrence_end, o.is_override "
                "FROM occurrences o "
                "JOIN components c ON c.id = o.component_id "
                "WHERE c.account_name = ? AND c.calendar_name = ? "
                "AND o.occurrence_start >= ? AND o.occurrence_start < ? "
                "ORDER BY o.occurrence_start",
                (
                    calendar.account_name,
                    calendar.calendar_name,
                    start_sql,
                    end_sql,
                ),
            )
            rows = cursor.fetchall()
        return tuple(_row_to_occurrence(r) for r in rows)

    def list_pending_pushes(self, calendar: CalendarRef) -> tuple[StoredComponent, ...]:
        with self.connection() as conn:
            cursor = conn.execute(
                f"SELECT {', '.join(_COMPONENT_COLUMNS)} FROM components "
                "WHERE account_name = ? AND calendar_name = ? "
                "AND href IS NULL AND local_status = 'active' "
                "ORDER BY uid, COALESCE(recurrence_id, '')",
                (calendar.account_name, calendar.calendar_name),
            )
            rows = cursor.fetchall()
        return tuple(_row_to_component(r) for r in rows)

    def list_calendar_components(
        self, calendar: CalendarRef
    ) -> tuple[StoredComponent, ...]:
        with self.connection() as conn:
            cursor = conn.execute(
                f"SELECT {', '.join(_COMPONENT_COLUMNS)} FROM components "
                "WHERE account_name = ? AND calendar_name = ? "
                "ORDER BY uid, COALESCE(recurrence_id, '')",
                (calendar.account_name, calendar.calendar_name),
            )
            rows = cursor.fetchall()
        return tuple(_row_to_component(r) for r in rows)

    def list_calendars(self) -> tuple[CalendarRef, ...]:
        """Distinct (account, calendar) pairs that have at least one
        component row. Source of truth for the MCP server's
        list_calendars tool."""
        with self.connection() as conn:
            cursor = conn.execute(
                "SELECT DISTINCT account_name, calendar_name FROM components "
                "ORDER BY account_name, calendar_name"
            )
            rows = cursor.fetchall()
        return tuple(CalendarRef(r[0], r[1]) for r in rows)

    def search(
        self, query: str, *, calendar: CalendarRef | None = None, limit: int = 50
    ) -> tuple[StoredComponent, ...]:
        if not query.strip():
            return ()
        params: list[object] = [query]
        sql = (
            f"SELECT {', '.join('c.' + c for c in _COMPONENT_COLUMNS)} "
            "FROM components c "
            "JOIN components_fts ON components_fts.rowid = c.id "
            "WHERE components_fts MATCH ? "
        )
        if calendar is not None:
            sql += "AND c.account_name = ? AND c.calendar_name = ? "
            params.extend([calendar.account_name, calendar.calendar_name])
        sql += "ORDER BY bm25(components_fts) LIMIT ?"
        params.append(limit)
        with self.connection() as conn:
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
        return tuple(_row_to_component(r) for r in rows)

    def get_sync_state(self, calendar: CalendarRef) -> SyncState | None:
        with self.connection() as conn:
            cursor = conn.execute(
                "SELECT ctag, sync_token, synced_at FROM calendar_sync_state "
                "WHERE account_name = ? AND calendar_name = ?",
                (calendar.account_name, calendar.calendar_name),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return SyncState(
            calendar=calendar,
            ctag=_opt_str(row[0]),
            sync_token=_opt_str(row[1]),
            synced_at=_sql_to_datetime(_opt_str(row[2])),
        )

    def set_sync_state(self, state: SyncState) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO calendar_sync_state "
                "(account_name, calendar_name, ctag, sync_token, synced_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(account_name, calendar_name) DO UPDATE SET "
                "ctag = excluded.ctag, "
                "sync_token = excluded.sync_token, "
                "synced_at = excluded.synced_at",
                (
                    state.calendar.account_name,
                    state.calendar.calendar_name,
                    state.ctag,
                    state.sync_token,
                    _datetime_to_sql(state.synced_at),
                ),
            )

    def clear_all_sync_state(self) -> int:
        """Drop every per-calendar sync token.

        Used by `chronos sync --force` to put every calendar back on
        the slow path: the next `_sync_calendar` sees no `prior_state`,
        falls into `_slow_path_reconcile`, re-runs `calendar_query` +
        `calendar_multiget`, and rebuilds the `occurrences` cache via
        `populate_occurrences`. Returns the number of rows removed
        (handy for the CLI to print a confirmation).
        """
        with self.connection() as conn:
            cursor = conn.execute("DELETE FROM calendar_sync_state")
            return cursor.rowcount or 0


def _find_component_id(conn: sqlite3.Connection, ref: ComponentRef) -> int | None:
    cursor = conn.execute(
        "SELECT id FROM components "
        "WHERE account_name = ? AND calendar_name = ? AND uid = ? "
        "AND COALESCE(recurrence_id, '') = COALESCE(?, '')",
        (ref.account_name, ref.calendar_name, ref.uid, ref.recurrence_id),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return cast(int, row[0])


def _invalidate_master_occurrences(conn: sqlite3.Connection, ref: ComponentRef) -> None:
    cursor = conn.execute(
        "SELECT id FROM components "
        "WHERE account_name = ? AND calendar_name = ? AND uid = ? "
        "AND recurrence_id IS NULL",
        (ref.account_name, ref.calendar_name, ref.uid),
    )
    row = cursor.fetchone()
    if row is None:
        return
    conn.execute("DELETE FROM occurrences WHERE component_id = ?", (cast(int, row[0]),))


def _row_to_occurrence(row: tuple[object, ...]) -> Occurrence:
    (
        account_name,
        calendar_name,
        uid,
        recurrence_id,
        occurrence_start,
        occurrence_end,
        is_override,
    ) = row
    ref = ComponentRef(
        account_name=cast(str, account_name),
        calendar_name=cast(str, calendar_name),
        uid=cast(str, uid),
        recurrence_id=_opt_str(recurrence_id),
    )
    start = _sql_to_datetime(_opt_str(occurrence_start))
    if start is None:
        raise AssertionError("occurrence_start must be non-null in DB")
    return Occurrence(
        ref=ref,
        start=start,
        end=_sql_to_datetime(_opt_str(occurrence_end)),
        recurrence_id=_opt_str(recurrence_id),
        is_override=bool(is_override),
    )


def _component_to_row(component: StoredComponent) -> dict[str, object]:
    kind = (
        ComponentKind.VEVENT if isinstance(component, VEvent) else ComponentKind.VTODO
    )
    dtend = component.dtend if isinstance(component, VEvent) else None
    due = component.due if isinstance(component, VTodo) else None
    return {
        "account_name": component.ref.account_name,
        "calendar_name": component.ref.calendar_name,
        "uid": component.ref.uid,
        "recurrence_id": component.ref.recurrence_id,
        "component_kind": kind.value,
        "href": component.href,
        "etag": component.etag,
        "raw_ics": component.raw_ics,
        "summary": component.summary,
        "description": component.description,
        "location": component.location,
        "dtstart": _datetime_to_sql(component.dtstart),
        "dtend": _datetime_to_sql(dtend),
        "due": _datetime_to_sql(due),
        "status": component.status,
        "local_flags": json.dumps(sorted(component.local_flags)),
        "server_flags": json.dumps(sorted(component.server_flags)),
        "local_status": component.local_status.value,
        "trashed_at": _datetime_to_sql(component.trashed_at),
        "synced_at": _datetime_to_sql(component.synced_at),
    }


def _row_to_component(row: tuple[object, ...]) -> StoredComponent:
    (
        account_name,
        calendar_name,
        uid,
        recurrence_id,
        component_kind,
        href,
        etag,
        raw_ics,
        summary,
        description,
        location,
        dtstart,
        dtend,
        due,
        status,
        local_flags,
        server_flags,
        local_status,
        trashed_at,
        synced_at,
    ) = row
    ref = ComponentRef(
        account_name=cast(str, account_name),
        calendar_name=cast(str, calendar_name),
        uid=cast(str, uid),
        recurrence_id=_opt_str(recurrence_id),
    )
    kind = ComponentKind(cast(str, component_kind))
    if kind == ComponentKind.VEVENT:
        return VEvent(
            ref=ref,
            href=_opt_str(href),
            etag=_opt_str(etag),
            raw_ics=cast(bytes, raw_ics),
            summary=_opt_str(summary),
            description=_opt_str(description),
            location=_opt_str(location),
            dtstart=_sql_to_datetime(_opt_str(dtstart)),
            dtend=_sql_to_datetime(_opt_str(dtend)),
            status=_opt_str(status),
            local_flags=frozenset(_decode_flags(local_flags)),
            server_flags=frozenset(_decode_flags(server_flags)),
            local_status=LocalStatus(cast(str, local_status)),
            trashed_at=_sql_to_datetime(_opt_str(trashed_at)),
            synced_at=_sql_to_datetime(_opt_str(synced_at)),
        )
    return VTodo(
        ref=ref,
        href=_opt_str(href),
        etag=_opt_str(etag),
        raw_ics=cast(bytes, raw_ics),
        summary=_opt_str(summary),
        description=_opt_str(description),
        location=_opt_str(location),
        dtstart=_sql_to_datetime(_opt_str(dtstart)),
        due=_sql_to_datetime(_opt_str(due)),
        status=_opt_str(status),
        local_flags=frozenset(_decode_flags(local_flags)),
        server_flags=frozenset(_decode_flags(server_flags)),
        local_status=LocalStatus(cast(str, local_status)),
        trashed_at=_sql_to_datetime(_opt_str(trashed_at)),
        synced_at=_sql_to_datetime(_opt_str(synced_at)),
    )


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    return cast(str, value)


def _decode_flags(value: object) -> Sequence[str]:
    if value is None:
        return ()
    raw = cast(str, value)
    parsed = cast(object, json.loads(raw))
    if not isinstance(parsed, list):
        return ()
    out: list[str] = []
    for item in cast(list[object], parsed):
        if isinstance(item, str):
            out.append(item)
    return tuple(out)


def _datetime_to_sql(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat()


def _sql_to_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)
