from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, RichLog, Static

from chronos.domain import SyncResult

SyncRunner = Callable[..., Sequence[SyncResult]]
"""Runner contract — accepts `cancel_event` keyword, returns per-account results."""


class SyncProgressScreen(ModalScreen[None]):
    """Foreground modal that runs sync and renders progress live.

    Shows a tail of the most recent `chronos.sync` log lines while the
    worker runs, exposes a Cancel button that flips a `threading.Event`
    the runner observes at calendar boundaries, and replaces itself
    with a summary + Close button when sync finishes (or fails).

    The dialog owns the cancel event and the worker lifecycle, so
    `MainScreen` no longer needs to track sync state.
    """

    BINDINGS = [
        Binding("escape", "close_or_cancel", "Cancel / close", show=False),
    ]

    def __init__(
        self,
        runner: SyncRunner,
        *,
        on_finished: Callable[[Sequence[SyncResult], BaseException | None], None],
    ) -> None:
        super().__init__()
        self._runner = runner
        self._on_finished = on_finished
        self._cancel_event = threading.Event()
        self._handler: _LogToScreenHandler | None = None
        self._results: tuple[SyncResult, ...] = ()
        self._error: BaseException | None = None
        self._state: str = "running"

    def compose(self) -> ComposeResult:
        with Vertical(id="sync-progress-box"):
            yield Label("Syncing", id="sync-progress-title", classes="dialog-title")
            # `RichLog` auto-scrolls as `write()` is called from
            # `_append_message`. `max_lines` caps the in-memory
            # backlog so a multi-thousand-event sync doesn't OOM.
            yield RichLog(
                id="sync-progress-log",
                max_lines=500,
                wrap=True,
                markup=False,
                highlight=False,
            )
            yield Static("", id="sync-progress-summary")
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="sync-action", variant="warning")

    def on_mount(self) -> None:
        # Hook the chronos logger so per-calendar / per-batch / per-
        # ingest INFO records show up in the dialog as they happen.
        # The handler is also why we don't need a custom progress
        # callback in the sync engine: it already logs everything we
        # want to display.
        self._handler = _LogToScreenHandler(self._on_log_record)
        logging.getLogger("chronos").addHandler(self._handler)
        self._run_in_worker()

    @work(thread=True, exclusive=True, group="chronos-sync")
    def _run_in_worker(self) -> None:
        try:
            results = self._runner(cancel_event=self._cancel_event)
        except BaseException as exc:  # noqa: BLE001 — surface every failure
            self.app.call_from_thread(self._on_done, (), exc)  # pyright: ignore[reportUnknownMemberType]
            return
        self.app.call_from_thread(self._on_done, results, None)  # pyright: ignore[reportUnknownMemberType]

    def _on_log_record(self, message: str) -> None:
        # `logging.Handler.emit` runs on whatever thread emitted the
        # record (the sync worker). Marshal to the UI thread before
        # touching widgets.
        self.app.call_from_thread(self._append_message, message)  # pyright: ignore[reportUnknownMemberType]

    def _append_message(self, message: str) -> None:
        self.query_one("#sync-progress-log", RichLog).write(message)

    def _on_done(
        self,
        results: Sequence[SyncResult],
        error: BaseException | None,
    ) -> None:
        if self._handler is not None:
            logging.getLogger("chronos").removeHandler(self._handler)
            self._handler.close()
            self._handler = None
        self._results = tuple(results)
        self._error = error
        self._state = "done"
        cancelled = self._cancel_event.is_set()

        title = self.query_one("#sync-progress-title", Label)
        if error is not None:
            title.update("Sync failed")
        elif cancelled:
            title.update("Sync cancelled")
        else:
            title.update("Sync complete")
        self.query_one("#sync-progress-summary", Static).update(self._summary())

        button = self.query_one("#sync-action", Button)
        button.label = "Close"
        button.variant = "default"
        button.disabled = False

    def _summary(self) -> str:
        if self._error is not None:
            return f"Error: {self._error}"
        added = sum(r.components_added for r in self._results)
        updated = sum(r.components_updated for r in self._results)
        removed = sum(r.components_removed for r in self._results)
        errors = sum(len(r.errors) for r in self._results)
        line = f"+{added} added  ~{updated} updated  -{removed} removed"
        if errors:
            line += f"   ({errors} error(s))"
        if not self._results:
            return "No accounts ran."
        return line

    def on_button_pressed(self, event: Button.Pressed) -> None:
        del event  # the dialog only has one button
        if self._state == "running":
            self._request_cancel()
            return
        self._dismiss_with_result()

    def action_close_or_cancel(self) -> None:
        # Esc behaves like the button: cancel while running, close
        # once the worker has reported back.
        if self._state == "running":
            self._request_cancel()
            return
        self._dismiss_with_result()

    def _request_cancel(self) -> None:
        if self._state != "running":
            return
        self._cancel_event.set()
        self._state = "cancelling"
        title = self.query_one("#sync-progress-title", Label)
        title.update("Cancelling sync…")
        button = self.query_one("#sync-action", Button)
        button.label = "Cancelling…"
        button.disabled = True

    def _dismiss_with_result(self) -> None:
        results = self._results
        error = self._error
        self.dismiss(None)
        self._on_finished(results, error)


class _LogToScreenHandler(logging.Handler):
    """Forward every formatted record to a callback.

    Set up at INFO so per-calendar / per-batch progress (which the
    sync engine already emits) flows in. DEBUG records would be too
    noisy for the dialog tail.
    """

    def __init__(self, callback: Callable[[str], None]) -> None:
        super().__init__(level=logging.INFO)
        self._callback = callback
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._callback(self.format(record))
        except Exception:  # noqa: BLE001 — never let a logging failure kill sync
            self.handleError(record)


__all__ = ["SyncProgressScreen"]
