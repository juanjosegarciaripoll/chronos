from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from chronos.credentials import CredentialResolutionError
from chronos.domain import AccountConfig, AppConfig, CalendarRef
from chronos.ical_parser import IcalParseError, parse_vcalendar
from chronos.protocols import CredentialsProvider, IndexRepository, MirrorRepository


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
) -> DoctorReport:
    results: list[DiagnosticResult] = []
    for account in config.accounts:
        results.append(_check_credentials(account, creds))
        results.extend(_check_mirror_integrity(account, mirror, index))
    return DoctorReport(results=tuple(results))


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
    "format_report",
    "run_doctor",
]
