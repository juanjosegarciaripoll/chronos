from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from textual import work
from textual.app import App

from chronos.domain import AlarmRecord, AppConfig, SyncResult
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

logger = logging.getLogger(__name__)

_ALARM_POLL_SECS = 30.0
# Reminders stay on screen long enough to be seen after looking away.
_ALARM_TOAST_SECS = 120.0
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
    #title-row { height: 1; }
    #view-title { width: 1fr; padding: 0 1; color: $text-muted; }
    #sync-status { width: auto; padding: 0 1; color: $text-muted; }
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
    .event-edit-title { text-style: bold; margin-bottom: 1; }
    .event-field-row, .event-datetime-row {
        height: 1;
        margin-bottom: 1;
        align-vertical: middle;
    }
    .event-field-label { width: 14; }
    .event-field-control { width: 1fr; }
    .event-date { width: 18; margin-right: 1; }
    .event-time { width: 14; }
    #edit-error { color: $error; }

    /* Modal dialogs: `align: center middle;` on the screen itself is
       Textual's stock idiom for centring a single child container.
       The dialog box then carries an explicit width so the centring
       has something to act on (auto-width inside a flex parent
       expands to fill, defeating the centre rule). */
    SyncConfirmScreen, ConfirmScreen, SyncProgressScreen,
    EventDetailScreen, OAuthProgressScreen, ImportIcsScreen, GotoScreen {
        align: center middle;
    }
    .dialog-box {
        padding: 1 2;
        height: auto;
        border: solid $primary;
    }
    .dialog-box .dialog-title {
        text-style: bold;
        margin-bottom: 1;
    }
    .dialog-box .dialog-actions {
        margin-top: 1;
        align-horizontal: right;
        /* `height: 1` matches the compact single-row buttons below;
           an explicit height keeps the action row from being squashed
           to zero when the dialog body is tall (e.g. a RichLog above)
           under the parent's `height: auto`. */
        height: 1;
    }
    /* Compact, borderless buttons in the Pony style: a single text row
       with the variant colour as background, no chunky Textual border. */
    .dialog-box .dialog-actions Button {
        margin: 0 1;
        height: 1;
        min-width: 0;
        border: none;
        padding: 0 1;
    }
    .dialog-box .dialog-actions Button:focus {
        text-style: reverse bold;
    }
    /* Per-dialog width and height overrides. */
    #event-detail    { width: 80; max-height: 80%; }
    #sync-confirm-box { width: 80; }
    #confirm-box     { width: 60; }
    #goto-box        { width: 60; }
    #goto-error      { color: $error; }
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

    def __init__(self, services: TuiServices, theme_name: str | None = None) -> None:
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
        self.set_interval(_ALARM_POLL_SECS, self._fire_pending_alarms, name="alarms")

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

    def _fire_pending_alarms(self) -> None:
        """Announce every alarm that fell due since the last poll.

        Runs every `_ALARM_POLL_SECS`. Alarms are delivered through the
        terminal, so they reach the user wherever the TUI is displayed —
        including over SSH, where a desktop notification would land on
        the remote machine: an in-app toast, the terminal bell, and an
        OSC 777 notification for terminals that turn it into a desktop
        one (terminals without support ignore it).
        """
        now = self.services.now()
        try:
            pending = self.services.index.query_pending_alarms(
                now - _ALARM_LOOKBACK, now
            )
        except Exception:  # noqa: BLE001
            logger.exception("query_pending_alarms failed")
            return
        fired = False
        for alarm in pending:
            if alarm.db_id is None:
                continue
            title = alarm.summary or "Chronos reminder"
            message = alarm_message(alarm, now)
            self.notify(message, title=title, timeout=_ALARM_TOAST_SECS)
            self._write_to_terminal(osc777_notification(title, message))
            try:
                self.services.index.mark_alarm_fired(alarm.db_id, now)
            except Exception:  # noqa: BLE001
                logger.exception("mark_alarm_fired failed for alarm %s", alarm.db_id)
            logger.info("alarm fired: %s (%s)", title, message)
            fired = True
        if fired:
            self.bell()

    def _write_to_terminal(self, data: str) -> None:
        """Send raw control sequences to the terminal, as `App.bell` does."""
        if not self.is_headless and self._driver is not None:
            self._driver.write(data)


# Google fills every VALARM with this DESCRIPTION; repeating it in the
# notification body says nothing the title doesn't.
_BOILERPLATE_ALARM_TEXT = "This is an event reminder"


def osc777_notification(title: str, body: str) -> str:
    """OSC 777 `notify` sequence asking the terminal for a desktop notification.

    Control characters would end the sequence early (and `;` in the
    title would shift the body), so both are flattened: newlines become
    " · " and the rest are dropped.
    """

    def clean(text: str) -> str:
        text = text.replace("\n", " · ")
        return "".join(ch for ch in text if ch >= " " and ch != "\x7f")

    return f"\x1b]777;notify;{clean(title).replace(';', ',')};{clean(body)}\x07"


def alarm_message(alarm: AlarmRecord, now: datetime) -> str:
    """Notification body: when the event starts, then any alarm text."""
    start = alarm.occurrence_start.astimezone()
    local_now = now.astimezone()
    if start.date() == local_now.date():
        when = start.strftime("%H:%M")
    else:
        when = start.strftime("%a %d %b %H:%M")
    verb = "Started" if alarm.occurrence_start <= now else "Starts"
    lines = [f"{verb} {when}"]
    description = (alarm.description or "").strip()
    if description and description != _BOILERPLATE_ALARM_TEXT:
        lines.append(description)
    return "\n".join(lines)


__all__ = ["ChronosApp", "SyncRunner", "TuiServices"]
