from __future__ import annotations

from time import monotonic

from textual.timer import Timer
from textual.widgets import Label

# Clock face marking a *scheduled* (not running) periodic sync. A plain
# geometric glyph rather than an emoji clock: emoji presentation is
# double-width in some terminals and would shift the label.
SCHEDULED_SYNC_MARK = "◷"

_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

# Granularity of the scheduled-sync countdown: it repaints this often and
# is rounded down to the same step, so it never claims precision it has
# not got.
COUNTDOWN_STEP_SECONDS = 30


def format_countdown(seconds: float) -> str:
    """Render *seconds* remaining as `M:SS`, or `H:MM:SS` past an hour.

    Rounded down to `COUNTDOWN_STEP_SECONDS`. A sync that is already due
    (or overdue, because the event loop was busy when the timer fired)
    reads `0:00` rather than a negative number.
    """
    total = max(0, int(seconds))
    total -= total % COUNTDOWN_STEP_SECONDS
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class SyncStatus(Label):
    """One-line indicator of the TUI's background sync.

    Shows a spinner while a background sync runs and, otherwise, a
    countdown to the next scheduled one. Blank while no periodic sync is
    scheduled and none is running.
    """

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 — Textual's kwarg name
        super().__init__("", id=id)
        self._syncing = False
        self._spinner_index = 0
        self._spinner_timer: Timer | None = None
        # `time.monotonic()` deadline of the next automatic sync.
        self._next_sync_deadline: float | None = None
        self._countdown_timer: Timer | None = None

    @property
    def syncing(self) -> bool:
        return self._syncing

    def set_syncing(self, active: bool) -> None:
        """Start or stop the spinner. Idempotent in both directions."""
        if active:
            if self._spinner_timer is not None:
                return
            self._syncing = True
            self._spinner_index = 0
            self._spinner_timer = self.set_interval(0.2, self._advance_spinner)
        else:
            if self._spinner_timer is not None:
                self._spinner_timer.stop()
                self._spinner_timer = None
            self._syncing = False
        self._repaint()

    def set_next_sync(self, deadline: float) -> None:
        """Show a countdown to the `time.monotonic()` *deadline*."""
        self._next_sync_deadline = deadline
        if self._countdown_timer is None:
            self._countdown_timer = self.set_interval(
                COUNTDOWN_STEP_SECONDS, self._repaint
            )
        self._repaint()

    def _advance_spinner(self) -> None:
        self._spinner_index += 1
        self._repaint()

    def _repaint(self) -> None:
        # A sync in flight outranks the countdown: while it runs, when
        # the *next* one is due is not what the user wants to read.
        if self._syncing:
            frame = _SPINNER_FRAMES[self._spinner_index % len(_SPINNER_FRAMES)]
            self.update(f"{frame} syncing…")
        elif self._next_sync_deadline is not None:
            remaining = format_countdown(self._next_sync_deadline - monotonic())
            self.update(f"{SCHEDULED_SYNC_MARK} {remaining}")
        else:
            self.update("")


__all__ = ["SyncStatus", "format_countdown"]
