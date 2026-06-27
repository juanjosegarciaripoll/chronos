from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from textual import work
from textual.app import App

from chronos.domain import AppConfig, SyncResult
from chronos.protocols import (
    CredentialsProvider,
    IndexRepository,
    MirrorRepository,
)
from chronos.tui.screens.main_screen import MainScreen
from chronos.tui.terminal import (
    pop_terminal_title,
    push_terminal_title,
    set_terminal_title,
)

_ALARM_POLL_SECS = 30.0
_ALARM_LOOKBACK = timedelta(minutes=15)

# Built-in Textual theme chosen when the user has not set one (config /
# --theme). flexoki is near-black-on-near-white, the highest-contrast of
# the bundled themes; override per-user via config or the --theme flag.
DEFAULT_THEME = "flexoki"

SyncRunner = Callable[..., Sequence[SyncResult]]
"""Runs every configured account's sync.

Called as `runner()` for a one-shot run, or `runner(cancel_event=evt)`
to allow the caller (the TUI worker) to interrupt mid-flight. The
runtime accepts the kwarg unconditionally; the simple test fakes that
ignore it are valid implementations of the Protocol.
"""


@dataclass
class TuiServices:
    """Dependencies the TUI needs.

    Constructed by the CLI entry-point and handed to `ChronosApp`. Tests
    inject fakes for everything except `now`. `sync_runner` is `None`
    when the TUI was launched from a context that has not wired sync
    in (most TUI tests); pressing the sync key in that mode shows a
    notification rather than running anything.
    """

    config: AppConfig
    mirror: MirrorRepository
    index: IndexRepository
    creds: CredentialsProvider
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    sync_runner: SyncRunner | None = None
    startup_ics_path: Path | None = None


class ChronosApp(App[None]):
    """Top-level Textual app.

    All real logic lives in `MainScreen`; the app is just a host. We
    push the main screen on mount instead of in `compose` so the
    constructor runs synchronously without touching any I/O.
    """

    # Ctrl-P opens Textual's built-in command palette, which includes the
    # "Change theme" picker — the live in-app theme switcher. Kept enabled
    # so users can raise contrast on the fly; the F1 help screen documents
    # it alongside chronos's own keybindings.
    ENABLE_COMMAND_PALETTE = True

    CSS = """
    #main-body { height: 1fr; }
    CalendarPanel { width: 30; border-right: solid $accent; }
    #centre-pane { width: 1fr; }
    #view-title { padding: 0 1; color: $text-muted; }
    EventList { height: 2fr; }
    /* The timeline takes the full centre-pane height in Day / Grid
       views — `MainScreen.refresh_view` toggles its `display` along
       with EventList / EventView based on the active view. */
    TimelineGrid { height: 1fr; background: $background; }
    #detail-pane {
        height: 1fr;
        border-top: solid $accent;
        padding: 1;
    }
    #event-edit, #search-dialog {
        padding: 1;
    }

    /* Modal dialogs: `align: center middle;` on the screen itself is
       Textual's stock idiom for centring a single child container.
       The dialog box then carries an explicit width so the centring
       has something to act on (auto-width inside a flex parent
       expands to fill, defeating the centre rule). */
    SyncConfirmScreen, ConfirmScreen, SyncProgressScreen,
    EventDetailScreen, OAuthProgressScreen, ImportIcsScreen {
        align: center middle;
    }
    .dialog-box {
        padding: 1 2;
        height: auto;
    }
    .dialog-box .dialog-title {
        margin-bottom: 1;
    }
    .dialog-box .dialog-actions {
        margin-top: 1;
        align-horizontal: right;
        /* `height: 3` reserves room for the standard Textual button
           row even when the dialog body is tall — `height: auto` on
           the parent + a tall RichLog above could otherwise squash
           the action row to zero. */
        height: 3;
    }
    .dialog-box .dialog-actions Button {
        margin-left: 1;
    }
    /* Per-dialog width and height overrides. */
    #event-detail    { width: 80; max-height: 80%; }
    #sync-confirm-box { width: 80; }
    #confirm-box     { width: 60; }
    #import-ics-box  { width: 80; }
    #sync-progress-box { width: 100; max-height: 80%; }
    #oauth-box       { width: 70; }
    #oauth-status    { margin-bottom: 1; }
    /* Scrollable progress log: bordered, scrolls automatically as
       new lines come in via `RichLog.write`. */
    #sync-progress-log {
        height: 18;
        padding: 0 1;
    }
    #sync-progress-summary {
        margin-top: 1;
    }
    """

    def __init__(
        self, services: TuiServices, theme_name: str | None = None
    ) -> None:
        super().__init__()
        self.services = services
        # The CLI resolves a concrete built-in theme (config / --theme /
        # DEFAULT_THEME), so a running app always has an explicit theme.
        # Tests that construct ChronosApp(services) keep Textual's default.
        if theme_name is not None:
            self.theme = theme_name

    def on_mount(self) -> None:
        push_terminal_title()
        set_terminal_title(f"Chronos {datetime.now().strftime('%d/%m/%Y')}")
        self.push_screen(MainScreen())  # pyright: ignore[reportUnknownMemberType]
        if not self.is_headless:
            self._start_mcp_server()
            self._start_alarm_worker()

    def on_unmount(self) -> None:
        pop_terminal_title()

    @work(exclusive=False, name="mcp-server", exit_on_error=False)
    async def _start_mcp_server(self) -> None:
        """Start the MCP TCP server on an ephemeral port.

        Runs for the lifetime of the TUI on the app's own event loop
        (no separate thread, per ai/MCP.md).  Failure is non-fatal:
        the TUI continues normally without MCP.
        """
        from chronos.mcp_server import start_tcp_server

        try:
            await start_tcp_server(
                index=self.services.index,
                mirror=self.services.mirror,
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"MCP TCP server stopped: {exc}")

    @work(exclusive=False, name="alarm-worker", exit_on_error=False)
    async def _start_alarm_worker(self) -> None:
        """Poll for due alarms every 30 seconds and fire OS desktop notifications.

        Imports ``desktop_notifier`` lazily so the TUI starts normally even
        when the library is unavailable.  All notification errors are swallowed
        — a broken notifier never brings down the TUI.
        """
        try:
            from desktop_notifier import DesktopNotifier  # type: ignore[import-untyped]

            notifier = DesktopNotifier(app_name="Chronos")
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"desktop-notifier unavailable: {exc}")
            return
        while True:
            await asyncio.sleep(_ALARM_POLL_SECS)
            try:
                await self._fire_pending_alarms(notifier)
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"alarm poll error: {exc}")

    async def _fire_pending_alarms(self, notifier: object) -> None:
        now = self.services.now()
        try:
            pending = self.services.index.query_pending_alarms(
                now - _ALARM_LOOKBACK, now
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"query_pending_alarms failed: {exc}")
            return
        for alarm in pending:
            if alarm.db_id is None:
                continue
            title = alarm.summary or "Chronos reminder"
            message = alarm.description or "Reminder"
            try:
                await notifier.send(title=title, message=message)  # type: ignore[attr-defined]
                self.services.index.mark_alarm_fired(alarm.db_id, now)
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"notification failed for alarm {alarm.db_id}: {exc}")


__all__ = ["ChronosApp", "SyncRunner", "TuiServices"]
