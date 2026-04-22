from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol

from chronos.domain import (
    AccountConfig,
    CalendarConfig,
    CalendarRef,
    ComponentRef,
    CredentialSpec,
    Occurrence,
    ResourceRef,
    StoredComponent,
    SyncResult,
    SyncState,
)


class CalDAVSession(Protocol):
    def discover_principal(self) -> str: ...

    def list_calendars(self, principal_url: str) -> Sequence[CalendarConfig]: ...

    def get_ctag(self, calendar_url: str) -> str | None: ...

    def calendar_query(
        self, calendar_url: str
    ) -> Sequence[tuple[str, str]]: ...  # (href, etag)

    def calendar_multiget(
        self, calendar_url: str, hrefs: Sequence[str]
    ) -> Sequence[tuple[str, str, bytes]]: ...  # (href, etag, ics)

    def put(
        self, href: str, ics: bytes, etag: str | None
    ) -> str: ...  # returns new etag

    def delete(self, href: str, etag: str) -> None: ...


class MirrorRepository(Protocol):
    def list_calendars(self, account_name: str) -> Sequence[str]: ...

    def list_resources(
        self, account_name: str, calendar_name: str
    ) -> Sequence[ResourceRef]: ...

    def read(self, ref: ResourceRef) -> bytes: ...

    def write(self, ref: ResourceRef, data: bytes) -> None: ...

    def delete(self, ref: ResourceRef) -> None: ...

    def move(self, source: ResourceRef, target: ResourceRef) -> None: ...


class IndexRepository(Protocol):
    def connection(self) -> AbstractContextManager[sqlite3.Connection]: ...

    def upsert_component(self, component: StoredComponent) -> None: ...

    def get_component(self, ref: ComponentRef) -> StoredComponent | None: ...

    def delete_component(self, ref: ComponentRef) -> None: ...

    def list_pending_pushes(
        self, calendar: CalendarRef
    ) -> Sequence[StoredComponent]: ...

    def list_calendar_components(
        self, calendar: CalendarRef
    ) -> Sequence[StoredComponent]: ...

    def get_sync_state(self, calendar: CalendarRef) -> SyncState | None: ...

    def set_sync_state(self, state: SyncState) -> None: ...

    def set_occurrences(
        self, ref: ComponentRef, occurrences: Sequence[Occurrence]
    ) -> None: ...

    def query_occurrences(
        self,
        calendar: CalendarRef,
        window_start: datetime,
        window_end: datetime,
    ) -> Sequence[Occurrence]: ...

    def close(self) -> None: ...


class CredentialsProvider(Protocol):
    def resolve(self, account_name: str, spec: CredentialSpec) -> str: ...


class SyncService(Protocol):
    def sync_account(self, account: AccountConfig) -> SyncResult: ...
