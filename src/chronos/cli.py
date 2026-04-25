from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from chronos.authorization import Authorization
from chronos.bootstrap import (
    IsInteractiveFn,
    PromptFn,
    default_is_interactive,
    default_prompt,
    offer_bootstrap,
    write_template,
)
from chronos.caldav_client import CalDAVError, CalDAVHttpSession
from chronos.config import ConfigError
from chronos.config import load as load_config
from chronos.config import save as save_config
from chronos.credentials import CredentialResolutionError, DefaultCredentialsProvider
from chronos.domain import (
    GOOGLE_CALDAV_URL,
    AccountConfig,
    AppConfig,
    CalendarRef,
    CommandCredential,
    ComponentRef,
    CredentialSpec,
    EnvCredential,
    GoogleCredential,
    LocalStatus,
    OAuthCredential,
    PlaintextCredential,
    ResourceRef,
    StoredComponent,
    SyncResult,
    VEvent,
    VTodo,
)
from chronos.index_store import SqliteIndexRepository
from chronos.locking import SyncLockError, acquire_sync_lock
from chronos.mutations import build_event_ics, generate_uid, trashed_copy
from chronos.oauth import (
    OAuthError,
    StoredTokens,
    run_loopback_flow,
    save_tokens,
)
from chronos.paths import (
    default_config_path,
    default_index_path,
    default_mirror_dir,
    default_mirror_path,
    oauth_token_path,
    sync_lock_path,
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
from chronos.sync import SyncCancelled, sync_account

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
    prompt: PromptFn | None = None,
    is_interactive: IsInteractiveFn | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    parser = _build_parser()
    args = parser.parse_args(argv)
    # `sync` defaults to INFO so the per-calendar / per-chunk progress
    # logger.info(...) calls are visible without forcing the user to
    # type `-v`. Other commands stay at WARNING (quiet by default).
    _configure_logging(
        args.verbose,
        err,
        default_level=logging.INFO if args.command == "sync" else logging.WARNING,
    )
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

    # Data commands need a config. If none exists, offer to bootstrap one
    # interactively; otherwise print a helpful message and exit. Tests
    # that inject a `context_factory` bring their own config and bypass
    # this check.
    if context_factory is None and not config_path.exists():
        return _handle_missing_config(
            out,
            err,
            config_path=config_path,
            prompt=prompt,
            open_editor=open_editor,
            is_interactive=is_interactive,
        )

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


def _handle_missing_config(
    stdout: TextIO,
    stderr: TextIO,
    *,
    config_path: Path,
    prompt: PromptFn | None,
    open_editor: EditorFn | None,
    is_interactive: IsInteractiveFn | None,
) -> int:
    interactive = (is_interactive or default_is_interactive)()
    if not interactive:
        stderr.write(
            f"config not found: {config_path}\n"
            f"Run `chronos init` to create one, then "
            f"`chronos account add ...` to configure an account.\n"
        )
        return 2
    return offer_bootstrap(
        stdout,
        stderr,
        config_path=config_path,
        prompt=prompt or default_prompt,
        open_editor=open_editor or _default_open_editor,
    )


def _default_context_factory(config_path: Path | None) -> CliContext:
    path = config_path or default_config_path()
    config = load_config(path)
    mirror = VdirMirrorRepository(user_data_dir() / "mirror")
    index = SqliteIndexRepository(default_index_path())
    return CliContext(
        config=config,
        mirror=mirror,
        index=index,
        creds=DefaultCredentialsProvider(
            interactive_authorizer=_default_cli_authorizer
        ),
        stdout=sys.stdout,
        stderr=sys.stderr,
        now=datetime.now(UTC),
    )


_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_LOG_DATEFMT = "%H:%M:%S"


class _DropH3DowngradeFilter(logging.Filter):
    """Drop urllib3's per-request "Retrying after MustDowngradeError" line.

    niquests advertises HTTP/3, urllib3 picks it up from the server's
    Alt-Svc header, then bails because it can't actually serve h3 —
    every CalDAV request retries on HTTP/2 and logs a WARNING. The
    retry is automatic and harmless; the warning just clutters the
    sync output. We still want every other urllib3 WARNING (real
    network errors, redirects, etc.).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return "MustDowngradeError" not in message


def _configure_logging(
    verbose_count: int,
    stream: TextIO,
    *,
    default_level: int = logging.WARNING,
) -> None:
    """Wire `logging` to stderr based on `-v` / `CHRONOS_LOG_LEVEL`.

    `default_level` is what we use when neither -v nor the env var is
    set — `cmd_sync` lifts it to INFO so the per-calendar progress
    messages are visible without typing -v.

    `-v` lifts to INFO regardless of the default, `-vv` to DEBUG. The
    `CHRONOS_LOG_LEVEL` env var (DEBUG/INFO/WARNING/ERROR) overrides
    everything so users can crank verbosity without re-typing flags.
    """
    env = os.environ.get("CHRONOS_LOG_LEVEL", "").upper().strip()
    if env in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        level = getattr(logging, env)
    elif verbose_count >= 2:
        level = logging.DEBUG
    elif verbose_count == 1:
        level = logging.INFO
    else:
        level = default_level
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
    handler.addFilter(_DropH3DowngradeFilter())
    root = logging.getLogger()
    # Clear pre-existing handlers so repeated `main()` calls (tests)
    # don't accumulate duplicates.
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # urllib3's per-request DEBUG noise is only useful at -vv; keep it
    # at WARNING for -v so INFO-level sync progress stays readable.
    if level > logging.DEBUG:
        logging.getLogger("urllib3").setLevel(logging.WARNING)


def _default_cli_authorizer(
    account_name: str, spec: OAuthCredential, _token_path: Path
) -> StoredTokens:
    """Run the OAuth loopback flow inline when sync hits an unauthorized account.

    Wired into `_default_context_factory` so plain `chronos sync` "just
    works" the first time: open the user's browser to the consent
    screen, capture the redirect on a random local port, exchange the
    code for tokens (the caller saves them).

    Refuses to prompt when stdin/stdout aren't a TTY — cron / scripted
    invocations get a clean error rather than silently blocking on a
    browser that may never open. Network and HTTP failures from the
    OAuth provider are surfaced loudly on stdout (not just stderr) so
    the user notices them right after the "authorization required"
    preface.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise OAuthError(
            f"account {account_name!r} needs OAuth authorization, but "
            "stdin/stdout aren't a TTY. Re-run from an interactive "
            "terminal."
        )
    sys.stdout.write(
        f"\n[{account_name}] OAuth authorization required. "
        "Opening your browser to the provider's consent screen...\n"
    )
    sys.stdout.flush()
    try:
        return _default_loopback_flow(spec, sys.stdout)
    except OAuthError as exc:
        sys.stdout.write(
            f"\n[{account_name}] OAuth setup failed: {exc}\n"
            "  - Verify client_id and client_secret in config.toml.\n"
            "  - For Google: the OAuth client must be of type 'Desktop "
            "app' (the same type Thunderbird uses); Web/TV types reject "
            "the loopback redirect.\n"
        )
        sys.stdout.flush()
        raise
    except Exception as exc:
        # Network/HTTP errors from niquests are not OAuthError; convert
        # them so the credentials provider's standard wrapping applies.
        sys.stdout.write(
            f"\n[{account_name}] OAuth setup failed (network/HTTP "
            f"error): {type(exc).__name__}: {exc}\n"
        )
        sys.stdout.flush()
        raise OAuthError(
            f"network/HTTP error reaching OAuth provider: {type(exc).__name__}: {exc}"
        ) from exc


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
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help=(
            "Increase log verbosity (-v INFO, -vv DEBUG). The "
            "CHRONOS_LOG_LEVEL env var is also honoured "
            "(DEBUG/INFO/WARNING/ERROR)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sync_p = sub.add_parser(
        "sync", help="Synchronise configured accounts with their servers."
    )
    sync_p.add_argument(
        "--force",
        action="store_true",
        help=(
            "Drop every calendar's stored CTag before syncing so this run "
            "re-enters the slow path for every calendar (re-fetches all "
            "resources and rebuilds the local occurrences cache). Use this "
            "to recover from a stale cache without manually editing SQLite."
        ),
    )

    reset_p = sub.add_parser(
        "reset",
        help=(
            "Delete the local SQLite index and vdir mirror so the next "
            "`chronos sync` rebuilds them from scratch. Configuration and "
            "OAuth tokens are preserved."
        ),
    )
    reset_p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt.",
    )

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

    sub.add_parser("tui", help="Launch the Textual UI.")

    sub.add_parser(
        "mcp",
        help="Run the read-only MCP server over stdio.",
    )

    sub.add_parser(
        "init",
        help="Write a minimal config.toml if none exists at the target path.",
    )

    account_p = sub.add_parser("account", help="Manage accounts in config.toml.")
    account_sub = account_p.add_subparsers(dest="account_cmd", required=True)

    account_add = account_sub.add_parser("add", help="Append a new account.")
    account_add.add_argument("--name", required=True)
    account_add.add_argument(
        "--url",
        default=None,
        help=(
            "CalDAV root URL. Required for every backend except 'google', "
            "which defaults to Google's CalDAV root."
        ),
    )
    account_add.add_argument(
        "--username",
        default=None,
        help=(
            "Account username. Required for every backend except 'google', "
            "where the OAuth identity supplies it and this can be omitted."
        ),
    )
    account_add.add_argument(
        "--credential-backend",
        choices=("plaintext", "env", "command", "oauth", "google"),
        required=True,
    )
    account_add.add_argument(
        "--credential-value",
        default=None,
        help=(
            "For plaintext: the password. For env: the variable name. "
            "For command: the command line (shlex-split). Unused for "
            "the oauth and google backends — pass --client-id + "
            "--client-secret instead."
        ),
    )
    account_add.add_argument(
        "--client-id",
        default=None,
        help=(
            "OAuth 2.0 client ID (required when --credential-backend is "
            "'oauth' or 'google')."
        ),
    )
    account_add.add_argument(
        "--client-secret",
        default=None,
        help=(
            "OAuth 2.0 client secret (required when --credential-backend "
            "is 'oauth' or 'google')."
        ),
    )
    account_add.add_argument(
        "--oauth-scope",
        default="https://www.googleapis.com/auth/calendar",
        help=(
            "OAuth scope; defaults to Google Calendar read+write. "
            "Ignored for the 'google' backend (which fixes the scope)."
        ),
    )
    account_add.add_argument(
        "--mirror-path",
        type=Path,
        default=None,
        help=(
            "Where to mirror this account's .ics files. Defaults to "
            "<user-data-dir>/mirror/<account-name>."
        ),
    )
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
        return cmd_sync(ctx, force=bool(getattr(args, "force", False)))
    if command == "reset":
        return cmd_reset(ctx, yes=bool(getattr(args, "yes", False)))
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
    if command == "tui":
        return cmd_tui(ctx)
    if command == "mcp":
        return cmd_mcp(ctx)
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
            client_id=args.client_id,
            client_secret=args.client_secret,
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


def cmd_sync(ctx: CliContext, *, force: bool = False) -> int:
    try:
        with acquire_sync_lock(sync_lock_path()):
            if force:
                cleared = ctx.index.clear_all_sync_state()
                ctx.stdout.write(
                    f"--force: cleared sync state for {cleared} calendar(s); "
                    "every calendar will re-enter the slow path.\n"
                )
            return _cmd_sync_locked(ctx)
    except SyncLockError as exc:
        ctx.stderr.write(f"{exc}\n")
        return 2


def _cmd_sync_locked(ctx: CliContext) -> int:
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
        except CalDAVError as exc:
            # The default factory may raise CalDAVError when constructing
            # the session — e.g. Google email discovery failing — so the
            # per-account loop must keep going instead of crashing.
            ctx.stderr.write(f"[{account.name}] CalDAV error: {exc}\n")
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


def cmd_reset(
    ctx: CliContext,
    *,
    yes: bool = False,
    index_path: Path | None = None,
    mirror_dir: Path | None = None,
) -> int:
    """Wipe the local index + mirror.

    This is the user-facing escape hatch for "my local cache is wedged
    and `--force` isn't enough" — it deletes the SQLite index (plus
    `-wal` / `-shm` sidecars) and the entire vdir mirror tree.
    Configuration and OAuth tokens are deliberately untouched, so the
    next `chronos sync` knows where to fetch from but starts with a
    blank slate.

    `index_path` and `mirror_dir` default to the platform-standard
    locations from `paths.py`; tests inject tmpdir-rooted overrides.
    """
    target_index = index_path or default_index_path()
    target_mirror = mirror_dir or default_mirror_dir()

    targets: list[Path] = []
    if target_index.exists():
        targets.append(target_index)
        for suffix in ("-wal", "-shm"):
            sidecar = target_index.with_name(target_index.name + suffix)
            if sidecar.exists():
                targets.append(sidecar)
    if target_mirror.exists():
        targets.append(target_mirror)

    if not targets:
        ctx.stdout.write("Nothing to reset (no local index or mirror found).\n")
        return 0

    ctx.stdout.write("Reset will delete:\n")
    for path in targets:
        ctx.stdout.write(f"  {path}\n")
    ctx.stdout.write(
        "Configuration and OAuth tokens are preserved. "
        "The next `chronos sync` will repopulate the index and mirror "
        "from scratch.\n"
    )

    if not yes:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            ctx.stderr.write(
                "Refusing to reset non-interactively. Pass --yes to confirm.\n"
            )
            return 1
        ctx.stdout.write("Type 'yes' to confirm: ")
        ctx.stdout.flush()
        answer = sys.stdin.readline().strip().lower()
        if answer != "yes":
            ctx.stdout.write("Cancelled.\n")
            return 1

    # Close any open connections / file handles so Windows lets us
    # delete the underlying files.
    ctx.index.close()

    for path in targets:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            # `-wal` / `-shm` may disappear between the targets snapshot
            # and the unlink: SQLite checkpoints + cleans them up the
            # moment we close the connection a few lines above.
            path.unlink(missing_ok=True)

    ctx.stdout.write("Reset complete. Run `chronos sync` to repopulate.\n")
    return 0


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
    new_ics = build_event_ics(current.ref.uid, new_summary, new_start, new_end, ctx.now)
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


def cmd_mcp(ctx: CliContext) -> int:
    """Run the read-only MCP server until the client disconnects.

    Stdio transport: stdin/stdout carry the MCP JSON-RPC stream, so
    nothing chronos-side may print to stdout (logging is on stderr,
    which we already configured by the time `_dispatch` runs).
    """
    # Imported lazily so the mcp dependency only loads when actually
    # running the server, keeping `chronos --help` and other commands
    # snappy.
    import anyio

    from chronos.mcp_server import serve_stdio

    anyio.run(lambda: serve_stdio(index=ctx.index))
    return 0


def cmd_tui(ctx: CliContext) -> int:
    # Imported lazily so `chronos --help` and other commands don't pull
    # Textual into the import graph.
    from chronos.tui import ChronosApp, TuiServices

    # The TUI owns the terminal, so the OAuth device flow can't print
    # to stdout. Swap in a creds provider whose authorizer surfaces a
    # clear "go authorize from CLI" message instead of blocking on a
    # prompt the user can't see.
    tui_ctx = dataclasses.replace(
        ctx,
        creds=DefaultCredentialsProvider(
            interactive_authorizer=_tui_unsupported_authorizer
        ),
    )
    services = TuiServices(
        config=tui_ctx.config,
        mirror=tui_ctx.mirror,
        index=tui_ctx.index,
        creds=tui_ctx.creds,
        now=lambda: tui_ctx.now,
        sync_runner=build_sync_runner(tui_ctx),
    )
    app = ChronosApp(services)
    with _redirect_logs_to_file():
        app.run()
    return 0


def _redirect_logs_to_file() -> _LogRedirector:
    """Route the root logger to `tui.log` for the duration of the TUI.

    Sync emits per-calendar / per-batch progress at INFO; with the
    default stderr handler still in place those lines paint over
    Textual's screen and corrupt the rendering. The file handler
    keeps the user's logs available (`tail -f $XDG_DATA_HOME/chronos/
    tui.log`) without touching stderr while the app owns the terminal.
    """
    log_path = user_data_dir() / "tui.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    return _LogRedirector(log_path)


class _LogRedirector:
    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path
        self._previous_handlers: list[logging.Handler] = []
        self._previous_level = logging.WARNING
        self._file_handler: logging.FileHandler | None = None

    def __enter__(self) -> _LogRedirector:
        root = logging.getLogger()
        self._previous_handlers = list(root.handlers)
        self._previous_level = root.level
        handler = logging.FileHandler(self._log_path, mode="a", encoding="utf-8")
        handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
        handler.addFilter(_DropH3DowngradeFilter())
        self._file_handler = handler
        root.handlers = [handler]
        # Sync's progress logs are at INFO; lift the root level so
        # they actually land in the file even if the parent CLI
        # invocation defaulted to WARNING.
        root.setLevel(min(self._previous_level, logging.INFO))
        return self

    def __exit__(self, *exc_info: object) -> None:
        root = logging.getLogger()
        if self._file_handler is not None:
            self._file_handler.close()
        root.handlers = self._previous_handlers
        root.setLevel(self._previous_level)


def _tui_unsupported_authorizer(
    account_name: str, _spec: OAuthCredential, _token_path: Path
) -> StoredTokens:
    raise OAuthError(
        f"account {account_name!r} needs OAuth authorization, but the "
        "TUI can't run the device flow inline. Quit the TUI and run "
        "`chronos sync` once to authorize, then come back."
    )


def build_sync_runner(
    ctx: CliContext,
) -> Callable[..., Sequence[SyncResult]]:
    """Closure that runs `sync_account` over every configured account.

    Mirrors `cmd_sync`, but returns the per-account `SyncResult`s
    instead of writing to stdout. Per-account exceptions are caught
    and reported in the result's `errors` tuple so a single bad
    credential doesn't take the whole sync down.

    The runner accepts an optional `cancel_event` keyword. The TUI
    sets this event from another thread to interrupt a long-running
    sync; `sync_account` checks it at calendar boundaries and raises
    `SyncCancelled`, which the runner translates into a per-account
    error rather than letting it tear through the worker.
    """
    factory = ctx.session_factory or _default_session_factory

    def run(*, cancel_event: threading.Event | None = None) -> Sequence[SyncResult]:
        try:
            with acquire_sync_lock(sync_lock_path()):
                return _run_locked(cancel_event)
        except SyncLockError as exc:
            # Surface the lock contention as a SyncResult so the TUI's
            # banner shows the message instead of crashing the worker.
            return (_failure_result("(sync)", str(exc)),)

    def _run_locked(
        cancel_event: threading.Event | None,
    ) -> Sequence[SyncResult]:
        results: list[SyncResult] = []
        for account in ctx.config.accounts:
            if cancel_event is not None and cancel_event.is_set():
                results.append(_failure_result(account.name, "sync cancelled"))
                continue
            try:
                auth = ctx.creds.build_auth(account)
            except CredentialResolutionError as exc:
                results.append(_failure_result(account.name, str(exc)))
                continue
            try:
                session = factory(account, auth)
            except NotImplementedError as exc:
                results.append(_failure_result(account.name, str(exc)))
                continue
            except CalDAVError as exc:
                results.append(_failure_result(account.name, f"CalDAV: {exc}"))
                continue
            try:
                result = sync_account(
                    account=account,
                    session=session,
                    mirror=ctx.mirror,
                    index=ctx.index,
                    now=ctx.now,
                    cancel_event=cancel_event,
                )
            except SyncCancelled:
                results.append(_failure_result(account.name, "sync cancelled"))
                continue
            except CalDAVError as exc:
                results.append(_failure_result(account.name, f"CalDAV: {exc}"))
                continue
            if auth.on_commit is not None:
                auth.on_commit()
            results.append(result)
        return tuple(results)

    return run


def _failure_result(account_name: str, message: str) -> SyncResult:
    return SyncResult(
        account_name=account_name,
        calendars_synced=0,
        components_added=0,
        components_updated=0,
        components_removed=0,
        errors=(message,),
    )


def cmd_init(stdout: TextIO, stderr: TextIO, *, config_path: Path) -> int:
    if config_path.exists():
        stderr.write(
            f"config already exists at {config_path}. "
            "Use `chronos config edit` to modify it.\n"
        )
        return 1
    write_template(config_path)
    stdout.write(
        f"Wrote template to {config_path}\n"
        "Edit it directly, or run `chronos account add ...` to populate.\n"
    )
    return 0


def cmd_account_add(
    stdout: TextIO,
    stderr: TextIO,
    *,
    config_path: Path,
    name: str,
    url: str | None,
    username: str | None,
    backend: str,
    value: str | None,
    client_id: str | None,
    client_secret: str | None,
    oauth_scope: str,
    mirror_path: Path | None,
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
            client_id=client_id,
            client_secret=client_secret,
            oauth_scope=oauth_scope,
        )
    except ValueError as exc:
        stderr.write(f"{exc}\n")
        return 2
    if isinstance(credential, GoogleCredential):
        resolved_url = url or GOOGLE_CALDAV_URL
        resolved_username = username or ""
    else:
        if not url:
            stderr.write(f"backend {backend!r} requires --url\n")
            return 2
        if not username:
            stderr.write(f"backend {backend!r} requires --username\n")
            return 2
        resolved_url = url
        resolved_username = username
    import re  # local import: avoid module-level coupling for one-shot CLI

    resolved_mirror_path = mirror_path or default_mirror_path(name)
    new_account = AccountConfig(
        name=name,
        url=resolved_url,
        username=resolved_username,
        credential=credential,
        mirror_path=resolved_mirror_path,
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
    auth_flow: Callable[[OAuthCredential, TextIO], StoredTokens] | None = None,
) -> int:
    """Re-run OAuth authorization for an account.

    Usually unnecessary — the first `chronos sync` for an unauthorized
    account auto-runs the same flow inline. Useful when the user wants
    to re-consent to new scopes, swap OAuth clients, or reset a revoked
    refresh token without waiting for the next sync to discover it.
    """
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
    if isinstance(credential, GoogleCredential):
        oauth_credential = OAuthCredential(
            client_id=credential.client_id,
            client_secret=credential.client_secret,
        )
    elif isinstance(credential, OAuthCredential):
        oauth_credential = credential
    else:
        stderr.write(
            f"account {account_name!r} does not use an OAuth backend "
            f"(has {type(credential).__name__}). `oauth authorize` is "
            "only meaningful for the oauth and google backends.\n"
        )
        return 2
    flow = auth_flow or _default_loopback_flow
    try:
        tokens = flow(oauth_credential, stdout)
    except OAuthError as exc:
        stderr.write(f"{exc}\n")
        return 1
    token_path = oauth_credential.token_path or oauth_token_path(account_name)
    save_tokens(token_path, tokens)
    stdout.write(f"Tokens saved to {token_path}\n")
    return 0


# Helpers ---------------------------------------------------------------------


def _default_open_editor(path: Path) -> None:
    cmd = [*pick_editor(), str(path)]
    subprocess.run(cmd, check=True)


def pick_editor(
    *,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> list[str]:
    """Resolve a command list for an interactive editor.

    Priority follows POSIX convention plus a platform-default fallback:

    1. `$VISUAL` (full-screen / GUI editors).
    2. `$EDITOR` (line editors).
    3. Platform default — `notepad` on Windows, `nano` if installed,
       else `vi` (POSIX-required, present on every Unix).
    """
    real_env = env if env is not None else os.environ
    real_platform = platform if platform is not None else sys.platform
    real_which = which if which is not None else shutil.which
    for var in ("VISUAL", "EDITOR"):
        value = real_env.get(var)
        if value:
            return shlex.split(value)
    if real_platform.startswith("win"):
        return ["notepad"]
    if real_which("nano") is not None:
        return ["nano"]
    return ["vi"]


def _build_credential(
    backend: str,
    *,
    value: str | None,
    client_id: str | None,
    client_secret: str | None,
    oauth_scope: str,
) -> CredentialSpec:
    if backend == "google":
        if not client_id or not client_secret:
            raise ValueError("google backend requires --client-id and --client-secret")
        return GoogleCredential(client_id=client_id, client_secret=client_secret)
    if backend == "oauth":
        if not client_id or not client_secret:
            raise ValueError("oauth backend requires --client-id and --client-secret")
        return OAuthCredential(
            client_id=client_id,
            client_secret=client_secret,
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
    if isinstance(spec, GoogleCredential):
        return "google"
    return "encrypted"


def _default_loopback_flow(credential: OAuthCredential, stdout: TextIO) -> StoredTokens:
    """Default flow for production: OAuth 2.0 loopback (RFC 8252 + PKCE)."""
    stdout.write(
        "Opening browser. If it doesn't open automatically, copy the URL "
        "printed above into your browser. Waiting for the redirect...\n"
    )
    stdout.flush()
    return run_loopback_flow(
        client_id=credential.client_id,
        client_secret=credential.client_secret,
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
