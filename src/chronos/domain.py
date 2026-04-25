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


@dataclass(frozen=True)
class OAuthCredential:
    """OAuth 2.0 client credentials for an account.

    `client_id` and `client_secret` are inlined in `config.toml` (per
    project decision); `token_path` is None when the default location
    under `paths.oauth_token_dir()` should be used, or an absolute
    path to override it.

    Access/refresh tokens live in a separate JSON file at `token_path`
    (or the resolved default), not in `config.toml`. `scope` defaults
    to Google Calendar read+write.
    """

    client_id: str
    client_secret: str
    scope: str = "https://www.googleapis.com/auth/calendar"
    token_path: Path | None = None


@dataclass(frozen=True)
class GoogleCredential:
    """Google-specific OAuth shorthand: only client_id + client_secret.

    Equivalent to `OAuthCredential` with the Google CalDAV scope and
    the default token path, but writeable in `config.toml` as a
    two-field block. The TOML reader also lets accounts using this
    backend omit `url` and `username` — they default to Google's
    CalDAV root and an empty display string respectively.
    """

    client_id: str
    client_secret: str


GOOGLE_CALDAV_URL = "https://apidata.googleusercontent.com/caldav/v2/"
GOOGLE_OAUTH_SCOPE = "https://www.googleapis.com/auth/calendar"


CredentialSpec = (
    PlaintextCredential
    | EnvCredential
    | CommandCredential
    | KeyringCredential
    | OAuthCredential
    | GoogleCredential
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
