from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from textual import work
from textual.app import App

from chronos.domain import AppConfig, SyncResult
from chronos.protocols import (
    CredentialsProvider,
    IndexRepository,
    MirrorRepository,
)
from chronos.tui.screens.main_screen import MainScreen

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


class ChronosApp(App[None]):
    """Top-level Textual app.

    All real logic lives in `MainScreen`; the app is just a host. We
    push the main screen on mount instead of in `compose` so the
    constructor runs synchronously without touching any I/O.
    """

    # Textual binds Ctrl-P to its built-in command palette by default;
    # chronos's keyboard surface is small and visible from the F1 help
    # screen, so the palette is more confusing than useful here.
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    #main-body { height: 1fr; }
    CalendarPanel { width: 30; border-right: solid $accent; }
    #centre-pane { width: 1fr; }
    #view-title { padding: 0 1; color: $text-muted; }
    EventList { height: 2fr; }
    /* The timeline takes the full centre-pane height in Day / Grid
       views — `MainScreen.refresh_view` toggles its `display` along
       with EventList / EventView based on the active view. */
    TimelineGrid { height: 1fr; }
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
    EventDetailScreen, OAuthProgressScreen {
        align: center middle;
    }
    .dialog-box {
        background: $surface;
        border: solid $accent;
        padding: 1 2;
        height: auto;
    }
    .dialog-box .dialog-title {
        text-style: bold;
        margin-bottom: 1;
    }
    .dialog-box .dialog-empty {
        color: $text-muted;
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
    #sync-progress-box { width: 100; max-height: 80%; }
    #oauth-box       { width: 70; }
    #oauth-status    { color: $text-muted; margin-bottom: 1; }
    /* Scrollable progress log: bordered, scrolls automatically as
       new lines come in via `RichLog.write`. */
    #sync-progress-log {
        height: 18;
        border: solid $accent;
        background: $boost;
        padding: 0 1;
    }
    #sync-progress-summary {
        margin-top: 1;
        text-style: bold;
    }
    """

    def __init__(self, services: TuiServices) -> None:
        super().__init__()
        self.services = services

    def on_mount(self) -> None:
        self.push_screen(MainScreen())  # pyright: ignore[reportUnknownMemberType]
        if not self.is_headless:
            self._start_mcp_server()

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


__all__ = ["ChronosApp", "SyncRunner", "TuiServices"]
