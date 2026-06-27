from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from chronos.authorization import Authorization
from chronos.credentials import CredentialResolutionError
from chronos.domain import (
    AccountConfig,
    AppConfig,
    CalendarRef,
    ComponentKind,
    StoredComponent,
    VEvent,
    VTodo,
)
from chronos.ical_parser import IcalParseError, parse_vcalendar
from chronos.protocols import (
    CalDAVSession,
    CredentialsProvider,
    IndexRepository,
    MirrorRepository,
)

SessionFactory = Callable[[AccountConfig, Authorization], CalDAVSession]

# Live progress sink for the remote probe. Each call is a single short
# step description, emitted *before* a potentially-blocking network call
# so a stall is attributable to the last line printed.
ProgressFn = Callable[[str], None]
_REMOTE_MULTIGET_SAMPLE_LIMIT = 20


class DiagnosticStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, kw_only=True)
class DiagnosticResult:
    check: str
    scope: str
    status: DiagnosticStatus
    message: str


@dataclass(frozen=True, kw_only=True)
class DoctorReport:
    results: tuple[DiagnosticResult, ...] = field(default_factory=tuple)

    @property
    def failed(self) -> tuple[DiagnosticResult, ...]:
        return tuple(r for r in self.results if r.status == DiagnosticStatus.FAIL)

    @property
    def warnings(self) -> tuple[DiagnosticResult, ...]:
        return tuple(r for r in self.results if r.status == DiagnosticStatus.WARN)

    @property
    def exit_code(self) -> int:
        if self.failed:
            return 1
        return 0


def run_doctor(
    *,
    config: AppConfig,
    mirror: MirrorRepository,
    index: IndexRepository,
    creds: CredentialsProvider,
    session_factory: SessionFactory | None = None,
    progress: ProgressFn | None = None,
) -> DoctorReport:
    emit = progress if progress is not None else _noop_progress
    results: list[DiagnosticResult] = []
    for account in config.accounts:
        results.append(_check_credentials(account, creds))
        results.extend(_check_mirror_integrity(account, mirror, index))
        if session_factory is not None:
            results.extend(
                _check_remote_caldav(account, creds, index, session_factory, emit)
            )
    return DoctorReport(results=tuple(results))


def _noop_progress(_message: str) -> None:
    return None


def _check_credentials(
    account: AccountConfig, creds: CredentialsProvider
) -> DiagnosticResult:
    try:
        creds.build_auth(account)
    except CredentialResolutionError as exc:
        return DiagnosticResult(
            check="credentials",
            scope=account.name,
            status=DiagnosticStatus.FAIL,
            message=str(exc),
        )
    return DiagnosticResult(
        check="credentials",
        scope=account.name,
        status=DiagnosticStatus.OK,
        message="credential resolved",
    )


def _check_mirror_integrity(
    account: AccountConfig,
    mirror: MirrorRepository,
    index: IndexRepository,
) -> list[DiagnosticResult]:
    results: list[DiagnosticResult] = []
    calendars = mirror.list_calendars(account.name)
    for calendar_name in calendars:
        scope = f"{account.name}/{calendar_name}"
        calendar_ref = CalendarRef(account.name, calendar_name)

        mirror_refs = mirror.list_resources(account.name, calendar_name)
        mirror_uids = {r.uid for r in mirror_refs}

        index_components = index.list_calendar_components(calendar_ref)
        index_uids = {c.ref.uid for c in index_components}

        orphans = mirror_uids - index_uids
        missing = {
            c.ref.uid
            for c in index_components
            if c.href is not None and c.ref.uid not in mirror_uids
        }
        unparseable: list[str] = []
        for ref in mirror_refs:
            try:
                parse_vcalendar(mirror.read(ref))
            except IcalParseError:
                unparseable.append(ref.uid)

        if not (orphans or missing or unparseable):
            results.append(
                DiagnosticResult(
                    check="mirror-integrity",
                    scope=scope,
                    status=DiagnosticStatus.OK,
                    message=(
                        f"{len(mirror_refs)} resources, "
                        f"{len(index_components)} index rows"
                    ),
                )
            )
            continue
        if unparseable:
            results.append(
                DiagnosticResult(
                    check="mirror-integrity",
                    scope=scope,
                    status=DiagnosticStatus.FAIL,
                    message=f"unparseable .ics: {sorted(unparseable)}",
                )
            )
        if missing:
            results.append(
                DiagnosticResult(
                    check="mirror-integrity",
                    scope=scope,
                    status=DiagnosticStatus.FAIL,
                    message=f"index rows point at missing mirror files: "
                    f"{sorted(missing)}",
                )
            )
        if orphans:
            results.append(
                DiagnosticResult(
                    check="mirror-integrity",
                    scope=scope,
                    status=DiagnosticStatus.WARN,
                    message=f"mirror files with no index row: {sorted(orphans)}",
                )
            )
    return results


def _check_remote_caldav(
    account: AccountConfig,
    creds: CredentialsProvider,
    index: IndexRepository,
    session_factory: SessionFactory,
    progress: ProgressFn = _noop_progress,
) -> list[DiagnosticResult]:
    """Probe remote CalDAV discovery without logging secrets or event bodies."""
    progress(f"{account.name}: resolving credentials")
    try:
        auth = creds.build_auth(account)
    except CredentialResolutionError as exc:
        return [
            DiagnosticResult(
                check="remote-caldav",
                scope=account.name,
                status=DiagnosticStatus.FAIL,
                message=f"credential unresolved: {_redact(str(exc))}",
            )
        ]

    try:
        progress(f"{account.name}: opening session")
        session = session_factory(account, auth)
        progress(f"{account.name}: discovering principal (PROPFIND)")
        principal = session.discover_principal()
        progress(f"{account.name}: listing calendars (PROPFIND Depth:1)")
        calendars = tuple(session.list_calendars(principal))
    except Exception as exc:
        return [
            DiagnosticResult(
                check="remote-caldav",
                scope=account.name,
                status=DiagnosticStatus.FAIL,
                message=(
                    f"discovery failed: {type(exc).__name__}: {_redact(str(exc))}"
                ),
            )
        ]

    scoped = tuple(
        calendar
        for calendar in calendars
        if any(pattern.fullmatch(calendar.name) for pattern in account.include)
        and not any(pattern.fullmatch(calendar.name) for pattern in account.exclude)
    )
    results: list[DiagnosticResult] = [
        DiagnosticResult(
            check="remote-caldav",
            scope=account.name,
            status=DiagnosticStatus.OK if scoped else DiagnosticStatus.WARN,
            message=(
                f"discovered {len(calendars)} calendar(s); "
                f"{len(scoped)} matched include/exclude filters"
            ),
        )
    ]

    progress(
        f"{account.name}: discovered {len(calendars)} calendar(s), {len(scoped)} scoped"
    )
    for calendar in scoped:
        scope = f"{account.name}/{calendar.name}"
        try:
            progress(f"{scope}: calendar-query (REPORT)")
            remote_resources = tuple(session.calendar_query(calendar.url))
        except Exception as exc:
            results.append(
                DiagnosticResult(
                    check="remote-calendar-query",
                    scope=scope,
                    status=DiagnosticStatus.FAIL,
                    message=(
                        f"calendar-query failed: {type(exc).__name__}: "
                        f"{_redact(str(exc))}"
                    ),
                )
            )
            continue

        local_rows = tuple(
            index.list_calendar_components(CalendarRef(account.name, calendar.name))
        )
        remote_count = len(remote_resources)
        results.append(
            DiagnosticResult(
                check="remote-calendar-query",
                scope=scope,
                status=DiagnosticStatus.OK if remote_count else DiagnosticStatus.WARN,
                message=(
                    f"calendar-query returned {remote_count} resource(s); "
                    f"local index has {_component_count_summary(local_rows)}"
                ),
            )
        )
        if remote_resources:
            sample_size = min(len(remote_resources), _REMOTE_MULTIGET_SAMPLE_LIMIT)
            progress(f"{scope}: multiget sample of {sample_size} resource(s) (REPORT)")
            results.append(
                _check_remote_multiget_sample(
                    session=session,
                    calendar_url=calendar.url,
                    scope=scope,
                    remote_resources=remote_resources,
                )
            )

    progress(f"{account.name}: remote probe complete")

    if auth.on_commit is not None:
        auth.on_commit()
    return results


@dataclass(frozen=True, kw_only=True)
class _SampleParseStats:
    events: int = 0
    todos: int = 0
    parse_errors: int = 0
    empty_or_unsupported: int = 0

    @property
    def parsed_components(self) -> int:
        return self.events + self.todos


def _check_remote_multiget_sample(
    *,
    session: CalDAVSession,
    calendar_url: str,
    scope: str,
    remote_resources: tuple[tuple[str, str], ...],
) -> DiagnosticResult:
    sample_hrefs = tuple(
        href for href, _etag in remote_resources[:_REMOTE_MULTIGET_SAMPLE_LIMIT]
    )
    try:
        fetched = tuple(session.calendar_multiget(calendar_url, sample_hrefs))
    except Exception as exc:
        return DiagnosticResult(
            check="remote-multiget-sample",
            scope=scope,
            status=DiagnosticStatus.FAIL,
            message=(
                f"calendar-multiget failed: {type(exc).__name__}: {_redact(str(exc))}"
            ),
        )

    stats = _sample_parse_stats(fetched)
    status = DiagnosticStatus.OK
    if (
        len(fetched) < len(sample_hrefs)
        or stats.parse_errors
        or stats.parsed_components == 0
    ):
        status = DiagnosticStatus.WARN

    return DiagnosticResult(
        check="remote-multiget-sample",
        scope=scope,
        status=status,
        message=(
            f"sampled {len(sample_hrefs)} of {len(remote_resources)} resource(s); "
            f"multiget returned {len(fetched)} body(ies); "
            f"parsed {stats.events} event component(s), "
            f"{stats.todos} task component(s); "
            f"{stats.parse_errors} parse error(s), "
            f"{stats.empty_or_unsupported} empty/unsupported object(s)"
        ),
    )


def _sample_parse_stats(
    fetched: tuple[tuple[str, str, bytes], ...],
) -> _SampleParseStats:
    events = 0
    todos = 0
    parse_errors = 0
    empty_or_unsupported = 0
    for _href, _etag, raw in fetched:
        try:
            parsed = parse_vcalendar(raw)
        except IcalParseError:
            parse_errors += 1
            continue
        if not parsed:
            empty_or_unsupported += 1
            continue
        for component in parsed:
            if component.kind == ComponentKind.VEVENT:
                events += 1
            elif component.kind == ComponentKind.VTODO:
                todos += 1
    return _SampleParseStats(
        events=events,
        todos=todos,
        parse_errors=parse_errors,
        empty_or_unsupported=empty_or_unsupported,
    )


def _component_count_summary(components: tuple[StoredComponent, ...]) -> str:
    events = sum(1 for component in components if isinstance(component, VEvent))
    todos = sum(1 for component in components if isinstance(component, VTodo))
    return f"{len(components)} row(s): {events} event(s), {todos} task(s)"


_URL_RE = re.compile(r"https?://[^\s)>\"]+")
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_SECRET_RE = re.compile(
    r"(?i)\b(authorization|cookie|set-cookie|access_token|refresh_token|"
    r"client_secret|password|passwd|token)=([^\s;&]+)"
)
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(authorization):\s*(?:(?:bearer|basic)\s+)?[^\s,;]+"
)
_COOKIE_HEADER_RE = re.compile(r"(?i)\b(cookie|set-cookie):\s*[^\r\n]+")


def _redact(text: str) -> str:
    text = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}: <redacted>", text)
    text = _COOKIE_HEADER_RE.sub(lambda m: f"{m.group(1)}: <redacted>", text)
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    text = _URL_RE.sub(lambda m: _fingerprint("url", m.group(0)), text)
    return _EMAIL_RE.sub(lambda m: _fingerprint("email", m.group(0)), text)


def _fingerprint(kind: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"<redacted-{kind}:{digest}>"


def format_report(report: DoctorReport) -> str:
    lines: list[str] = []
    for result in report.results:
        lines.append(
            f"[{result.status.value.upper():<4}] {result.check} "
            f"({result.scope}): {result.message}"
        )
    if not report.results:
        lines.append("No accounts configured; nothing to check.")
    return "\n".join(lines) + "\n"


__all__ = [
    "DiagnosticResult",
    "DiagnosticStatus",
    "DoctorReport",
    "ProgressFn",
    "SessionFactory",
    "format_report",
    "run_doctor",
]
