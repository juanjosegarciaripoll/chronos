from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from chronos.authorization import Authorization
from chronos.caldav_client import CalDAVError, CalDAVHttpSession
from chronos.config import ConfigError
from chronos.config import load as load_config
from chronos.config import save as save_config
from chronos.credentials import CredentialResolutionError, DefaultCredentialsProvider
from chronos.domain import (
    AccountConfig,
    AppConfig,
    CalendarRef,
    CommandCredential,
    ComponentRef,
    CredentialSpec,
    EnvCredential,
    LocalStatus,
    OAuthCredential,
    PlaintextCredential,
    ResourceRef,
    StoredComponent,
    VEvent,
    VTodo,
)
from chronos.index_store import SqliteIndexRepository
from chronos.mutations import build_event_ics, generate_uid, trashed_copy
from chronos.oauth import (
    OAuthError,
    StoredTokens,
    poll_for_tokens,
    request_device_code,
    save_tokens,
)
from chronos.paths import (
    default_config_path,
    default_index_path,
    oauth_token_path,
    user_data_dir,
)
from chronos.protocols import (
    CalDAVSession,
    CredentialsProvider,
    IndexRepository,
    MirrorRepository,
)
from chronos.services import format_report, run_doctor
from chronos.storage import VdirMirrorRepository
from chronos.sync import sync_account

SessionFactory = Callable[[AccountConfig, Authorization], CalDAVSession]
ContextFactory = Callable[[Path | None], "CliContext"]
EditorFn = Callable[[Path], None]


@dataclass
class CliContext:
    config: AppConfig
    mirror: MirrorRepository
    index: IndexRepository
    creds: CredentialsProvider
    stdout: TextIO
    stderr: TextIO
    now: datetime
    session_factory: SessionFactory | None = None


def main(
    argv: Sequence[str] | None = None,
    *,
    context_factory: ContextFactory | None = None,
    open_editor: EditorFn | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    parser = _build_parser()
    args = parser.parse_args(argv)
    config_path: Path = args.config or default_config_path()

    # Config-editing commands operate on the TOML file without needing the
    # mirror / index / credential plumbing.
    if args.command == "init":
        return cmd_init(out, err, config_path=config_path)
    if args.command == "account":
        return _dispatch_account(args, out, err, config_path=config_path)
    if args.command == "config":
        return _dispatch_config(
            args, out, err, config_path=config_path, open_editor=open_editor
        )
    if args.command == "oauth":
        return _dispatch_oauth(args, out, err, config_path=config_path)

    # Data commands need a full context.
    owns_context = context_factory is None
    factory = context_factory or _default_context_factory
    try:
        ctx = factory(args.config)
    except ConfigError as exc:
        err.write(f"config error: {exc}\n")
        return 2
    try:
        return _dispatch(args, ctx)
    finally:
        if owns_context:
            ctx.index.close()


def _default_context_factory(config_path: Path | None) -> CliContext:
    path = config_path or default_config_path()
    config = load_config(path)
    mirror = VdirMirrorRepository(user_data_dir() / "mirror")
    index = SqliteIndexRepository(default_index_path())
    return CliContext(
        config=config,
        mirror=mirror,
        index=index,
        creds=DefaultCredentialsProvider(),
        stdout=sys.stdout,
        stderr=sys.stderr,
        now=datetime.now(UTC),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chronos", description="Terminal-first calendar client."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.toml (defaults to platform user-config dir).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sync", help="Synchronise configured accounts with their servers.")

    list_p = sub.add_parser("list", help="List events and todos.")
    list_p.add_argument("--account", default=None)
    list_p.add_argument("--calendar", default=None)
    list_p.add_argument("--limit", type=int, default=50)
    list_p.add_argument("--since", type=_parse_dt, default=None)
    list_p.add_argument("--until", type=_parse_dt, default=None)

    show_p = sub.add_parser("show", help="Show one component by UID.")
    show_p.add_argument("uid")

    add_p = sub.add_parser("add", help="Add a new VEVENT.")
    add_p.add_argument("--account", required=True)
    add_p.add_argument("--calendar", required=True)
    add_p.add_argument("--summary", required=True)
    add_p.add_argument("--start", type=_parse_dt, required=True)
    add_p.add_argument("--end", type=_parse_dt, default=None)
    add_p.add_argument("--uid", default=None)

    edit_p = sub.add_parser("edit", help="Edit an existing VEVENT (local-only in v1).")
    edit_p.add_argument("uid")
    edit_p.add_argument("--summary", default=None)
    edit_p.add_argument("--start", type=_parse_dt, default=None)
    edit_p.add_argument("--end", type=_parse_dt, default=None)

    rm_p = sub.add_parser("rm", help="Mark a component as trashed.")
    rm_p.add_argument("uid")

    sub.add_parser("doctor", help="Run diagnostics on the local state.")

    sub.add_parser(
        "init",
        help="Write a minimal config.toml if none exists at the target path.",
    )

    account_p = sub.add_parser("account", help="Manage accounts in config.toml.")
    account_sub = account_p.add_subparsers(dest="account_cmd", required=True)

    account_add = account_sub.add_parser("add", help="Append a new account.")
    account_add.add_argument("--name", required=True)
    account_add.add_argument("--url", required=True)
    account_add.add_argument("--username", required=True)
    account_add.add_argument(
        "--credential-backend",
        choices=("plaintext", "env", "command", "oauth"),
        required=True,
    )
    account_add.add_argument(
        "--credential-value",
        default=None,
        help=(
            "For plaintext: the password. For env: the variable name. "
            "For command: the command line (shlex-split). Unused for "
            "the oauth backend — use --oauth-client-id + "
            "--oauth-client-secret instead."
        ),
    )
    account_add.add_argument(
        "--oauth-client-id",
        default=None,
        help="OAuth 2.0 client ID (required when --credential-backend=oauth).",
    )
    account_add.add_argument(
        "--oauth-client-secret",
        default=None,
        help="OAuth 2.0 client secret (required when --credential-backend=oauth).",
    )
    account_add.add_argument(
        "--oauth-scope",
        default="https://www.googleapis.com/auth/calendar",
        help="OAuth scope; defaults to Google Calendar read+write.",
    )
    account_add.add_argument("--mirror-path", type=Path, required=True)
    account_add.add_argument("--trash-retention-days", type=int, default=30)

    account_sub.add_parser("list", help="Show configured accounts.")

    account_rm = account_sub.add_parser("rm", help="Remove an account by name.")
    account_rm.add_argument("name")

    config_p = sub.add_parser("config", help="Manage config.toml.")
    config_sub = config_p.add_subparsers(dest="config_cmd", required=True)
    config_sub.add_parser(
        "edit",
        help="Open config.toml in $EDITOR; validate and save on close.",
    )

    oauth_p = sub.add_parser(
        "oauth",
        help="OAuth 2.0 authorisation flows for accounts.",
    )
    oauth_sub = oauth_p.add_subparsers(dest="oauth_cmd", required=True)
    oauth_authorize = oauth_sub.add_parser(
        "authorize",
        help="Run the OAuth device flow for an account and save tokens.",
    )
    oauth_authorize.add_argument("--account", required=True)

    return parser


def _dispatch(args: argparse.Namespace, ctx: CliContext) -> int:
    command = str(args.command)
    if command == "sync":
        return cmd_sync(ctx)
    if command == "list":
        return cmd_list(
            ctx,
            account=args.account,
            calendar=args.calendar,
            limit=args.limit,
            since=args.since,
            until=args.until,
        )
    if command == "show":
        return cmd_show(ctx, uid=args.uid)
    if command == "add":
        return cmd_add(
            ctx,
            account_name=args.account,
            calendar_name=args.calendar,
            summary=args.summary,
            start=args.start,
            end=args.end,
            uid=args.uid,
        )
    if command == "edit":
        return cmd_edit(
            ctx,
            uid=args.uid,
            summary=args.summary,
            start=args.start,
            end=args.end,
        )
    if command == "rm":
        return cmd_rm(ctx, uid=args.uid)
    if command == "doctor":
        return cmd_doctor(ctx)
    ctx.stderr.write(f"unknown command: {command}\n")
    return 2


def _dispatch_account(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
    *,
    config_path: Path,
) -> int:
    sub = str(args.account_cmd)
    if sub == "add":
        return cmd_account_add(
            stdout,
            stderr,
            config_path=config_path,
            name=args.name,
            url=args.url,
            username=args.username,
            backend=args.credential_backend,
            value=args.credential_value,
            oauth_client_id=args.oauth_client_id,
            oauth_client_secret=args.oauth_client_secret,
            oauth_scope=args.oauth_scope,
            mirror_path=args.mirror_path,
            trash_retention_days=args.trash_retention_days,
        )
    if sub == "list":
        return cmd_account_list(stdout, stderr, config_path=config_path)
    if sub == "rm":
        return cmd_account_rm(stdout, stderr, config_path=config_path, name=args.name)
    stderr.write(f"unknown account subcommand: {sub}\n")
    return 2


def _dispatch_config(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
    *,
    config_path: Path,
    open_editor: EditorFn | None,
) -> int:
    sub = str(args.config_cmd)
    if sub == "edit":
        return cmd_config_edit(
            stdout,
            stderr,
            config_path=config_path,
            open_editor=open_editor or _default_open_editor,
        )
    stderr.write(f"unknown config subcommand: {sub}\n")
    return 2


def _dispatch_oauth(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
    *,
    config_path: Path,
) -> int:
    sub = str(args.oauth_cmd)
    if sub == "authorize":
        return cmd_oauth_authorize(
            stdout, stderr, config_path=config_path, account_name=args.account
        )
    stderr.write(f"unknown oauth subcommand: {sub}\n")
    return 2


# Commands --------------------------------------------------------------------


def cmd_sync(ctx: CliContext) -> int:
    factory = ctx.session_factory or _default_session_factory
    fails = 0
    for account in ctx.config.accounts:
        try:
            auth = ctx.creds.build_auth(account)
        except CredentialResolutionError as exc:
            ctx.stderr.write(f"[{account.name}] {exc}\n")
            fails += 1
            continue
        try:
            session = factory(account, auth)
        except NotImplementedError as exc:
            ctx.stderr.write(f"[{account.name}] {exc}\n")
            fails += 1
            continue
        try:
            result = sync_account(
                account=account,
                session=session,
                mirror=ctx.mirror,
                index=ctx.index,
                now=ctx.now,
            )
        except CalDAVError as exc:
            ctx.stderr.write(f"[{account.name}] CalDAV error: {exc}\n")
            fails += 1
            continue
        # Give the auth strategy a chance to persist rotated state
        # (OAuth access tokens refreshed during sync). Basic auth
        # leaves this unset.
        if auth.on_commit is not None:
            auth.on_commit()
        ctx.stdout.write(
            f"{account.name}: {result.calendars_synced} calendars "
            f"(+{result.components_added} "
            f"~{result.components_updated} "
            f"-{result.components_removed})\n"
        )
        for err in result.errors:
            ctx.stderr.write(f"[{account.name}] {err}\n")
    return 1 if fails else 0


def cmd_list(
    ctx: CliContext,
    *,
    account: str | None,
    calendar: str | None,
    limit: int,
    since: datetime | None,
    until: datetime | None,
) -> int:
    components = _collect_components(ctx, account=account, calendar=calendar)
    components = [c for c in components if c.local_status == LocalStatus.ACTIVE]
    if since is not None:
        components = [c for c in components if c.dtstart and c.dtstart >= since]
    if until is not None:
        components = [c for c in components if c.dtstart and c.dtstart < until]
    components.sort(key=_sort_key)
    for component in components[:limit]:
        ctx.stdout.write(_format_row(component) + "\n")
    return 0


def cmd_show(ctx: CliContext, *, uid: str) -> int:
    matches = _find_by_uid(ctx, uid)
    if not matches:
        ctx.stderr.write(f"not found: {uid}\n")
        return 1
    if len(matches) > 1:
        ctx.stderr.write(f"ambiguous uid {uid!r} matches multiple calendars:\n")
        for match in matches:
            ctx.stderr.write(f"  {match.ref.account_name}/{match.ref.calendar_name}\n")
        return 2
    _render_detail(matches[0], ctx.stdout)
    return 0


def cmd_add(
    ctx: CliContext,
    *,
    account_name: str,
    calendar_name: str,
    summary: str,
    start: datetime,
    end: datetime | None,
    uid: str | None,
) -> int:
    if not any(a.name == account_name for a in ctx.config.accounts):
        ctx.stderr.write(f"unknown account: {account_name}\n")
        return 2
    resolved_uid = uid or generate_uid(
        account_name, calendar_name, summary, start, ctx.now
    )
    ics = build_event_ics(resolved_uid, summary, start, end, ctx.now)
    ctx.mirror.write(ResourceRef(account_name, calendar_name, resolved_uid), ics)
    ref = ComponentRef(account_name, calendar_name, resolved_uid)
    component = VEvent(
        ref=ref,
        href=None,
        etag=None,
        raw_ics=ics,
        summary=summary,
        description=None,
        location=None,
        dtstart=start,
        dtend=end,
        status=None,
        local_flags=frozenset(),
        server_flags=frozenset(),
        local_status=LocalStatus.ACTIVE,
        trashed_at=None,
        synced_at=None,
    )
    ctx.index.upsert_component(component)
    ctx.stdout.write(f"{resolved_uid}\n")
    return 0


def cmd_edit(
    ctx: CliContext,
    *,
    uid: str,
    summary: str | None,
    start: datetime | None,
    end: datetime | None,
) -> int:
    matches = _find_by_uid(ctx, uid)
    if not matches:
        ctx.stderr.write(f"not found: {uid}\n")
        return 1
    if len(matches) > 1:
        ctx.stderr.write(f"ambiguous uid {uid!r}\n")
        return 2
    current = matches[0]
    if not isinstance(current, VEvent):
        ctx.stderr.write("edit: only VEVENT is supported in v1\n")
        return 2
    new_summary = summary if summary is not None else (current.summary or "")
    new_start = start if start is not None else current.dtstart
    new_end = end if end is not None else current.dtend
    if new_start is None:
        ctx.stderr.write("edit: missing DTSTART\n")
        return 2
    new_ics = build_event_ics(
        current.ref.uid, new_summary, new_start, new_end, ctx.now
    )
    ctx.mirror.write(current.ref.resource, new_ics)
    updated = VEvent(
        ref=current.ref,
        href=current.href,
        etag=current.etag,
        raw_ics=new_ics,
        summary=new_summary,
        description=current.description,
        location=current.location,
        dtstart=new_start,
        dtend=new_end,
        status=current.status,
        local_flags=current.local_flags,
        server_flags=current.server_flags,
        local_status=current.local_status,
        trashed_at=current.trashed_at,
        synced_at=current.synced_at,
    )
    ctx.index.upsert_component(updated)
    ctx.stdout.write(f"{current.ref.uid}\n")
    if current.href is not None:
        ctx.stderr.write(
            "warning: local-only edit; server push of edits is a v2 feature.\n"
        )
    return 0


def cmd_rm(ctx: CliContext, *, uid: str) -> int:
    matches = _find_by_uid(ctx, uid)
    if not matches:
        ctx.stderr.write(f"not found: {uid}\n")
        return 1
    for component in matches:
        trashed = trashed_copy(component, trashed_at=ctx.now)
        ctx.index.upsert_component(trashed)
    ctx.stdout.write(f"trashed {len(matches)}\n")
    return 0


def cmd_doctor(ctx: CliContext) -> int:
    report = run_doctor(
        config=ctx.config,
        mirror=ctx.mirror,
        index=ctx.index,
        creds=ctx.creds,
    )
    ctx.stdout.write(format_report(report))
    return report.exit_code


def cmd_init(stdout: TextIO, stderr: TextIO, *, config_path: Path) -> int:
    if config_path.exists():
        stderr.write(
            f"config already exists at {config_path}. "
            "Use `chronos config edit` to modify it.\n"
        )
        return 1
    minimal = AppConfig(
        config_version=1,
        use_utf8=False,
        editor=None,
        accounts=(),
    )
    save_config(minimal, config_path)
    stdout.write(f"Wrote {config_path}\n")
    return 0


def cmd_account_add(
    stdout: TextIO,
    stderr: TextIO,
    *,
    config_path: Path,
    name: str,
    url: str,
    username: str,
    backend: str,
    value: str | None,
    oauth_client_id: str | None,
    oauth_client_secret: str | None,
    oauth_scope: str,
    mirror_path: Path,
    trash_retention_days: int,
) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        stderr.write(f"{exc}\n")
        return 2
    if any(a.name == name for a in config.accounts):
        stderr.write(f"account already exists: {name}\n")
        return 1
    try:
        credential = _build_credential(
            backend,
            value=value,
            oauth_client_id=oauth_client_id,
            oauth_client_secret=oauth_client_secret,
            oauth_scope=oauth_scope,
        )
    except ValueError as exc:
        stderr.write(f"{exc}\n")
        return 2
    import re  # local import: avoid module-level coupling for one-shot CLI

    new_account = AccountConfig(
        name=name,
        url=url,
        username=username,
        credential=credential,
        mirror_path=mirror_path,
        trash_retention_days=trash_retention_days,
        include=(re.compile(".*"),),
        exclude=(),
        read_only=(),
    )
    updated = AppConfig(
        config_version=config.config_version,
        use_utf8=config.use_utf8,
        editor=config.editor,
        accounts=(*config.accounts, new_account),
    )
    save_config(updated, config_path)
    stdout.write(f"Added account {name} to {config_path}\n")
    return 0


def cmd_account_list(stdout: TextIO, stderr: TextIO, *, config_path: Path) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        stderr.write(f"{exc}\n")
        return 2
    if not config.accounts:
        stdout.write("(no accounts configured)\n")
        return 0
    for account in config.accounts:
        backend = _credential_backend(account.credential)
        stdout.write(
            f"{account.name}\t{account.url}\t{account.username}\tbackend={backend}\n"
        )
    return 0


def cmd_account_rm(
    stdout: TextIO,
    stderr: TextIO,
    *,
    config_path: Path,
    name: str,
) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        stderr.write(f"{exc}\n")
        return 2
    remaining = tuple(a for a in config.accounts if a.name != name)
    if len(remaining) == len(config.accounts):
        stderr.write(f"account not found: {name}\n")
        return 1
    updated = AppConfig(
        config_version=config.config_version,
        use_utf8=config.use_utf8,
        editor=config.editor,
        accounts=remaining,
    )
    save_config(updated, config_path)
    stdout.write(f"Removed account {name} from {config_path}\n")
    return 0


def cmd_config_edit(
    stdout: TextIO,
    stderr: TextIO,
    *,
    config_path: Path,
    open_editor: EditorFn,
) -> int:
    if not config_path.exists():
        stderr.write(f"config not found: {config_path}. Run `chronos init` first.\n")
        return 1
    # Copy the current contents into a temp file; the user edits there.
    # On validation success we atomically replace the original; on failure
    # the original is untouched.
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix="chronos-edit-",
        suffix=".toml",
        dir=config_path.parent,
        delete=False,
    ) as tmp:
        tmp.write(config_path.read_bytes())
        tmp_path = Path(tmp.name)
    try:
        try:
            open_editor(tmp_path)
        except subprocess.CalledProcessError:
            stderr.write("editor exited non-zero; config unchanged.\n")
            return 1
        except FileNotFoundError as exc:
            stderr.write(f"editor not found: {exc}\n")
            return 1
        try:
            config = load_config(tmp_path)
        except ConfigError as exc:
            stderr.write(f"config parse error: {exc}\n")
            stderr.write("Original config left unchanged.\n")
            return 1
        save_config(config, config_path)
        stdout.write(f"Saved {config_path}\n")
        return 0
    finally:
        tmp_path.unlink(missing_ok=True)


def cmd_oauth_authorize(
    stdout: TextIO,
    stderr: TextIO,
    *,
    config_path: Path,
    account_name: str,
    device_flow: Callable[[OAuthCredential, TextIO], StoredTokens] | None = None,
) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        stderr.write(f"{exc}\n")
        return 2
    account = next((a for a in config.accounts if a.name == account_name), None)
    if account is None:
        stderr.write(f"account not found: {account_name}\n")
        return 1
    credential = account.credential
    if not isinstance(credential, OAuthCredential):
        stderr.write(
            f"account {account_name!r} does not use the oauth backend "
            f"(has {type(credential).__name__}). `oauth authorize` is "
            "only meaningful for oauth accounts.\n"
        )
        return 2
    flow = device_flow or _default_device_flow
    try:
        tokens = flow(credential, stdout)
    except OAuthError as exc:
        stderr.write(f"{exc}\n")
        return 1
    token_path = credential.token_path or oauth_token_path(account_name)
    save_tokens(token_path, tokens)
    stdout.write(f"Tokens saved to {token_path}\n")
    return 0


# Helpers ---------------------------------------------------------------------


def _default_open_editor(path: Path) -> None:
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        raise FileNotFoundError(
            "Neither $EDITOR nor $VISUAL is set; cannot launch an editor."
        )
    cmd = [*shlex.split(editor), str(path)]
    subprocess.run(cmd, check=True)


def _build_credential(
    backend: str,
    *,
    value: str | None,
    oauth_client_id: str | None,
    oauth_client_secret: str | None,
    oauth_scope: str,
) -> CredentialSpec:
    if backend == "oauth":
        if not oauth_client_id or not oauth_client_secret:
            raise ValueError(
                "oauth backend requires --oauth-client-id and --oauth-client-secret"
            )
        return OAuthCredential(
            client_id=oauth_client_id,
            client_secret=oauth_client_secret,
            scope=oauth_scope,
        )
    if value is None:
        raise ValueError(f"backend {backend!r} requires --credential-value")
    if backend == "plaintext":
        return PlaintextCredential(password=value)
    if backend == "env":
        return EnvCredential(variable=value)
    if backend == "command":
        return CommandCredential(command=tuple(shlex.split(value)))
    raise ValueError(f"unknown credential backend: {backend}")


def _credential_backend(spec: CredentialSpec) -> str:
    if isinstance(spec, PlaintextCredential):
        return "plaintext"
    if isinstance(spec, EnvCredential):
        return "env"
    if isinstance(spec, CommandCredential):
        return "command"
    if isinstance(spec, OAuthCredential):
        return "oauth"
    return "encrypted"


def _default_device_flow(credential: OAuthCredential, stdout: TextIO) -> StoredTokens:
    grant = request_device_code(client_id=credential.client_id, scope=credential.scope)
    stdout.write(
        f"\nOpen {grant.verification_url} on any device and enter this code:\n"
        f"\n    {grant.user_code}\n\n"
        "Waiting for authorisation...\n"
    )
    stdout.flush()
    return poll_for_tokens(
        client_id=credential.client_id,
        client_secret=credential.client_secret,
        grant=grant,
        scope=credential.scope,
    )


def _default_session_factory(
    account: AccountConfig, authorization: Authorization
) -> CalDAVSession:
    return CalDAVHttpSession(url=account.url, authorization=authorization)


def _collect_components(
    ctx: CliContext, *, account: str | None, calendar: str | None
) -> list[StoredComponent]:
    out: list[StoredComponent] = []
    for acct in ctx.config.accounts:
        if account is not None and acct.name != account:
            continue
        for cal_name in ctx.mirror.list_calendars(acct.name):
            if calendar is not None and cal_name != calendar:
                continue
            out.extend(
                ctx.index.list_calendar_components(CalendarRef(acct.name, cal_name))
            )
    return out


def _find_by_uid(ctx: CliContext, uid: str) -> list[StoredComponent]:
    matches: list[StoredComponent] = []
    for acct in ctx.config.accounts:
        for cal_name in ctx.mirror.list_calendars(acct.name):
            ref = ComponentRef(acct.name, cal_name, uid)
            component = ctx.index.get_component(ref)
            if component is not None:
                matches.append(component)
    return matches


def _parse_dt(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _sort_key(component: StoredComponent) -> datetime:
    return component.dtstart or datetime.max.replace(tzinfo=UTC)


def _format_row(component: StoredComponent) -> str:
    start = component.dtstart.isoformat() if component.dtstart else "?"
    kind = "EVENT" if isinstance(component, VEvent) else "TODO "
    summary = component.summary or "(no summary)"
    return f"{start}  {kind}  {component.ref.uid:40s}  {summary}"


def _render_detail(component: StoredComponent, stdout: TextIO) -> None:
    stdout.write(f"UID: {component.ref.uid}\n")
    stdout.write(f"Account: {component.ref.account_name}\n")
    stdout.write(f"Calendar: {component.ref.calendar_name}\n")
    stdout.write(f"Kind: {'VEVENT' if isinstance(component, VEvent) else 'VTODO'}\n")
    stdout.write(f"Summary: {component.summary or ''}\n")
    if component.description:
        stdout.write(f"Description: {component.description}\n")
    if component.location:
        stdout.write(f"Location: {component.location}\n")
    if component.dtstart:
        stdout.write(f"Start: {component.dtstart.isoformat()}\n")
    if isinstance(component, VEvent) and component.dtend:
        stdout.write(f"End: {component.dtend.isoformat()}\n")
    if isinstance(component, VTodo) and component.due:
        stdout.write(f"Due: {component.due.isoformat()}\n")
    if component.status:
        stdout.write(f"Status: {component.status}\n")
    stdout.write(f"LocalStatus: {component.local_status.value}\n")
    if component.href:
        stdout.write(f"Href: {component.href}\n")
    if component.etag:
        stdout.write(f"ETag: {component.etag}\n")


__all__ = [
    "CliContext",
    "SessionFactory",
    "cmd_add",
    "cmd_doctor",
    "cmd_edit",
    "cmd_list",
    "cmd_rm",
    "cmd_show",
    "cmd_sync",
    "main",
]
