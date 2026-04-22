from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class ComponentKind(StrEnum):
    VEVENT = "VEVENT"
    VTODO = "VTODO"


class LocalStatus(StrEnum):
    ACTIVE = "active"
    TRASHED = "trashed"


@dataclass(frozen=True)
class PlaintextCredential:
    password: str


@dataclass(frozen=True)
class EnvCredential:
    variable: str


@dataclass(frozen=True)
class CommandCredential:
    command: tuple[str, ...]


@dataclass(frozen=True)
class KeyringCredential:
    service: str
    username: str


CredentialSpec = (
    PlaintextCredential | EnvCredential | CommandCredential | KeyringCredential
)


@dataclass(frozen=True, kw_only=True)
class AccountConfig:
    name: str
    url: str
    username: str
    credential: CredentialSpec
    mirror_path: Path
    trash_retention_days: int
    include: tuple[re.Pattern[str], ...]
    exclude: tuple[re.Pattern[str], ...]
    read_only: tuple[re.Pattern[str], ...]


@dataclass(frozen=True, kw_only=True)
class CalendarConfig:
    account_name: str
    calendar_name: str
    url: str
    read_only: bool
    supported_components: frozenset[ComponentKind]


@dataclass(frozen=True, kw_only=True)
class RemoteCalendar:
    name: str
    url: str
    supported_components: frozenset[ComponentKind]


@dataclass(frozen=True, kw_only=True)
class AppConfig:
    config_version: int
    use_utf8: bool
    editor: str | None
    accounts: tuple[AccountConfig, ...]


@dataclass(frozen=True)
class CalendarRef:
    account_name: str
    calendar_name: str


@dataclass(frozen=True)
class ResourceRef:
    account_name: str
    calendar_name: str
    uid: str

    @property
    def calendar(self) -> CalendarRef:
        return CalendarRef(self.account_name, self.calendar_name)


@dataclass(frozen=True)
class ComponentRef:
    account_name: str
    calendar_name: str
    uid: str
    recurrence_id: str | None = None

    @property
    def calendar(self) -> CalendarRef:
        return CalendarRef(self.account_name, self.calendar_name)

    @property
    def resource(self) -> ResourceRef:
        return ResourceRef(self.account_name, self.calendar_name, self.uid)


@dataclass(frozen=True, kw_only=True)
class VEvent:
    ref: ComponentRef
    href: str | None
    etag: str | None
    raw_ics: bytes
    summary: str | None
    description: str | None
    location: str | None
    dtstart: datetime | None
    dtend: datetime | None
    status: str | None
    local_flags: frozenset[str]
    server_flags: frozenset[str]
    local_status: LocalStatus
    trashed_at: datetime | None
    synced_at: datetime | None


@dataclass(frozen=True, kw_only=True)
class VTodo:
    ref: ComponentRef
    href: str | None
    etag: str | None
    raw_ics: bytes
    summary: str | None
    description: str | None
    location: str | None
    dtstart: datetime | None
    due: datetime | None
    status: str | None
    local_flags: frozenset[str]
    server_flags: frozenset[str]
    local_status: LocalStatus
    trashed_at: datetime | None
    synced_at: datetime | None


StoredComponent = VEvent | VTodo


@dataclass(frozen=True, kw_only=True)
class Occurrence:
    ref: ComponentRef
    start: datetime
    end: datetime | None
    recurrence_id: str | None
    is_override: bool


@dataclass(frozen=True, kw_only=True)
class SyncState:
    calendar: CalendarRef
    ctag: str | None
    sync_token: str | None
    synced_at: datetime | None


@dataclass(frozen=True, kw_only=True)
class SyncResult:
    account_name: str
    calendars_synced: int
    components_added: int
    components_updated: int
    components_removed: int
    errors: tuple[str, ...]
