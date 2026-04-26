from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, timedelta
from typing import TYPE_CHECKING, cast

from dateutil.relativedelta import relativedelta
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Label

from chronos.domain import (
    CalendarRef,
    ComponentRef,
    LocalStatus,
    ResourceRef,
    StoredComponent,
    SyncResult,
    VEvent,
)
from chronos.mutations import build_event_ics, generate_uid, trashed_copy
from chronos.paths import default_tui_state_path
from chronos.recurrence import expand
from chronos.tui.bindings import main_bindings
from chronos.tui.screens.agenda_screen import (
    rows_for as agenda_rows,
)
from chronos.tui.screens.agenda_screen import (
    title_for as agenda_title,
)
from chronos.tui.screens.confirm_screen import ConfirmScreen
from chronos.tui.screens.day_view_screen import (
    rows_for as day_rows,
)
from chronos.tui.screens.day_view_screen import (
    title_for as day_title,
)
from chronos.tui.screens.event_detail_screen import EventDetailScreen
from chronos.tui.screens.event_edit_screen import EditDraft, EventEditScreen
from chronos.tui.screens.grid_view_screen import (
    DEFAULT_GRID_DAYS,
)
from chronos.tui.screens.grid_view_screen import (
    rows_for as grid_rows,
)
from chronos.tui.screens.grid_view_screen import (
    title_for as grid_title,
)
from chronos.tui.screens.help_screen import HelpScreen
from chronos.tui.screens.search_dialog_screen import SearchDialogScreen
from chronos.tui.screens.sync_confirm_screen import SyncConfirmScreen
from chronos.tui.screens.sync_progress_screen import SyncProgressScreen
from chronos.tui.views import (
    AgendaWindow,
    CalendarSelection,
    OccurrenceRow,
    ViewKind,
    all_calendar_refs,
)
from chronos.tui.widgets.calendar_panel import CalendarPanel
from chronos.tui.widgets.event_list import EventList
from chronos.tui.widgets.event_view import EventView
from chronos.tui.widgets.timeline_grid import TimelineGrid

if TYPE_CHECKING:
    from chronos.tui.app import ChronosApp, TuiServices


# Mirrors `sync._OCCURRENCE_WINDOW_PAST` / `_OCCURRENCE_WINDOW_FUTURE`.
# Kept local instead of importing because the local-edit path doesn't
# otherwise depend on the sync engine and we don't want to pull
# `chronos.sync` into the TUI's import graph.
_LOCAL_OCCURRENCE_WINDOW_PAST = timedelta(days=365 * 30)
_LOCAL_OCCURRENCE_WINDOW_FUTURE = timedelta(days=365 * 5)


class MainScreen(Screen[None]):
    """The single screen the user spends 99% of their time in.

    Three panes: calendar tree (left), view list (centre), detail
    (right). View-switch and global actions are bound here per
    `CONVENTIONS.md §11`.
    """

    BINDINGS = main_bindings()

    def __init__(self) -> None:
        super().__init__()
        self._view: ViewKind = ViewKind.AGENDA
        # Within the Agenda view, `d`/`w`/`m` flip between
        # day / week / month windows. Defaults to week — a useful
        # at-a-glance horizon for most users.
        self._agenda_window: AgendaWindow = AgendaWindow.WEEK
        self._viewed_date: date = date(2026, 4, 25)  # rebound in on_mount
        # Multi-day grid chunk size. Phase 4 will swap this for a
        # terminal-width-aware choice (3 when narrow, 4 when wide).
        self._grid_days: int = DEFAULT_GRID_DAYS
        self._selection = CalendarSelection(refs=frozenset())
        self._last_rows: tuple[OccurrenceRow, ...] = ()

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-body"):
            yield CalendarPanel(on_selection_change=self._on_calendar_selection)
            with Vertical(id="centre-pane"):
                yield Label("", id="view-title")
                yield EventList(id="centre-list")
                yield TimelineGrid(id="centre-timeline")
                yield EventView(id="detail-pane")
        yield Footer()

    def on_mount(self) -> None:
        services = self._services()
        self._viewed_date = services.now().date()
        panel: CalendarPanel = self.query_one(CalendarPanel)
        panel.populate(all_calendar_refs(services.config, services.mirror))
        # The calendar tree is hidden by default — the agenda already
        # shows everything, and a permanent left-hand panel chews up
        # horizontal real estate that the timeline views need. Pressing
        # `c` reveals it.
        panel.display = False
        # Render in AGENDA first so EventList is visible when focus is set.
        # Calling focus() on a hidden widget is a silent no-op in Textual,
        # which would leave the timeline unfocused in Day / Grid views.
        self.refresh_view()
        self.query_one(EventList).focus()
        # Now switch to the persisted view (if different from AGENDA).
        # Hiding the now-focused EventList causes Textual to auto-redirect
        # focus to the next focusable widget — the timeline.
        saved = _load_last_view()
        if saved != ViewKind.AGENDA:
            self._set_view(saved)

    def action_toggle_calendars(self) -> None:
        panel = self.query_one(CalendarPanel)
        panel.display = not panel.display
        if panel.display:
            panel.focus()
        else:
            self.query_one(EventList).focus()

    def _on_calendar_selection(self, selection: CalendarSelection) -> None:
        self._selection = selection
        self.refresh_view()

    # View switches ----------------------------------------------------------

    def _set_view(self, kind: ViewKind) -> None:
        self._view = kind
        _save_last_view(kind)
        self.refresh_view()
        # Give focus to the primary interactive widget of the new view so
        # keyboard navigation works immediately without a manual Tab.
        # call_after_refresh defers the focus until after the DataTable has
        # finished re-rendering its rows (focusing mid-render can be stolen).
        if kind == ViewKind.AGENDA:
            self.call_after_refresh(  # pyright: ignore[reportUnknownMemberType]
                self.query_one(EventList).focus
            )
        else:
            self.call_after_refresh(  # pyright: ignore[reportUnknownMemberType]
                self.query_one(TimelineGrid).focus
            )

    def action_view_agenda(self) -> None:
        self._set_view(ViewKind.AGENDA)

    def action_view_day(self) -> None:
        self._set_view(ViewKind.DAY)

    def action_view_grid(self) -> None:
        self._set_view(ViewKind.GRID)

    # Agenda window tuners ----------------------------------------------------

    def action_agenda_window_day(self) -> None:
        if self._view != ViewKind.AGENDA:
            return
        self._agenda_window = AgendaWindow.DAY
        self.refresh_view()

    def action_agenda_window_week(self) -> None:
        if self._view != ViewKind.AGENDA:
            return
        self._agenda_window = AgendaWindow.WEEK
        self.refresh_view()

    def action_agenda_window_month(self) -> None:
        if self._view != ViewKind.AGENDA:
            return
        self._agenda_window = AgendaWindow.MONTH
        self.refresh_view()

    # Date-axis navigation ----------------------------------------------------

    def action_today(self) -> None:
        self._viewed_date = self._services().now().date()
        self.refresh_view()

    def action_next_day(self) -> None:
        self._step_natural(direction=+1)

    def action_prev_day(self) -> None:
        self._step_natural(direction=-1)

    def action_next_chunk(self) -> None:
        if self._view != ViewKind.GRID:
            return
        self._viewed_date = self._viewed_date + timedelta(days=self._grid_days)
        self.refresh_view()

    def action_prev_chunk(self) -> None:
        if self._view != ViewKind.GRID:
            return
        self._viewed_date = self._viewed_date - timedelta(days=self._grid_days)
        self.refresh_view()

    def _step_natural(self, *, direction: int) -> None:
        """Advance / retreat the viewed date by the natural unit of
        the current view.

        - Day / Grid: 1 day.
        - Agenda Day window: 1 day.
        - Agenda Week window: 7 days.
        - Agenda Month window: 1 calendar month (`relativedelta`
          handles month-end clamps — Jan 31 → Feb 28, etc.).
        """
        if self._view == ViewKind.AGENDA:
            if self._agenda_window == AgendaWindow.DAY:
                self._viewed_date = self._viewed_date + timedelta(days=direction)
            elif self._agenda_window == AgendaWindow.WEEK:
                self._viewed_date = self._viewed_date + timedelta(days=7 * direction)
            else:  # MONTH
                self._viewed_date = self._viewed_date + relativedelta(months=direction)
        elif self._view in (ViewKind.DAY, ViewKind.GRID):
            self._viewed_date = self._viewed_date + timedelta(days=direction)
        else:
            return
        self.refresh_view()

    def action_quit(self) -> None:
        # Textual's screen-level binding dispatch does NOT fall through
        # to App.action_quit when the screen has no `action_quit`. The
        # binding fires, the action lookup misses, and the press is
        # silently dropped. Defining it locally is the only reliable fix.
        self.app.exit()  # pyright: ignore[reportUnknownMemberType]

    # Mutating actions -------------------------------------------------------

    def action_new_event(self) -> None:
        services = self._services()
        calendars = all_calendar_refs(services.config, services.mirror)
        if not calendars:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                "No calendars available. Add an account first."
            )
            return
        default = self._first_selected(calendars)
        screen = EventEditScreen(
            calendars=calendars,
            existing=None,
            default_calendar=default,
            on_save=self._save_event,
        )
        self.app.push_screen(screen)  # pyright: ignore[reportUnknownMemberType]

    def action_edit_event(self) -> None:
        component = self._currently_selected_component()
        if component is None:
            return
        self._edit_specific(component)

    def action_open_event(self) -> None:
        component = self._currently_selected_component()
        if component is None:
            return
        self._open_specific(component)

    def action_delete_event(self) -> None:
        component = self._currently_selected_component()
        if component is None:
            return
        self.delete_with_confirm(component)

    def delete_with_confirm(self, component: StoredComponent) -> None:
        # Confirmation flow flips the component to LocalStatus.TRASHED.
        # The next sync's `_push_trashed` then issues the server DELETE
        # and purges the local row, so a user-initiated delete here is
        # picked up automatically by `chronos sync`.
        prompt = f"Delete {component.summary or component.ref.uid!r}?"
        confirm = ConfirmScreen(prompt, lambda: self._trash(component))
        self.app.push_screen(confirm)  # pyright: ignore[reportUnknownMemberType]

    def action_show_help(self) -> None:
        self.app.push_screen(HelpScreen(main_bindings()))  # pyright: ignore[reportUnknownMemberType]

    def action_sync(self) -> None:
        services = self._services()
        screen = SyncConfirmScreen(services.config.accounts, self._run_sync)
        self.app.push_screen(screen)  # pyright: ignore[reportUnknownMemberType]

    def action_search(self) -> None:
        services = self._services()
        components: list[StoredComponent] = []
        for ref in all_calendar_refs(services.config, services.mirror):
            components.extend(services.index.list_calendar_components(ref))
        screen = SearchDialogScreen(components, on_select=self._open_specific)
        self.app.push_screen(screen)  # pyright: ignore[reportUnknownMemberType]

    # Internals --------------------------------------------------------------

    def refresh_view(self) -> None:
        services = self._services()
        calendars = all_calendar_refs(services.config, services.mirror)
        title_label: Label = self.query_one("#view-title", Label)
        event_list: EventList = self.query_one(EventList)
        timeline: TimelineGrid = self.query_one(TimelineGrid)
        detail: EventView = self.query_one(EventView)
        # Friendly date labels (Today / Tomorrow / weekday) are anchored
        # on the user's actual today, not on the viewed date — looking
        # at a 2014 day still shows the absolute date, not "Today".
        now = services.now()
        today = now.date()
        if self._view == ViewKind.AGENDA:
            title_label.update(agenda_title(self._viewed_date, self._agenda_window))
            rows = agenda_rows(
                index=services.index,
                calendars=calendars,
                selection=self._selection,
                viewed=self._viewed_date,
                mode=self._agenda_window,
            )
            self._last_rows = rows
            # Agenda layout: compact list on top, inline detail pane
            # on the bottom. Timeline is hidden.
            event_list.display = True
            timeline.display = False
            detail.display = True
            event_list.show_events(rows, today=today, now=now, compact=True)
            self._refresh_detail()
            return

        # Day / Grid: timeline takes the centre. The list and the
        # inline detail pane both go away — the detail pane only
        # appears when the user explicitly opens an entry (Enter on
        # a cell), via the modal `EventDetailScreen`.
        event_list.display = False
        detail.display = False
        timeline.display = True

        if self._view == ViewKind.DAY:
            title_label.update(day_title(self._viewed_date))
            rows = day_rows(
                index=services.index,
                calendars=calendars,
                selection=self._selection,
                viewed=self._viewed_date,
            )
            timeline.show_days([(self._viewed_date, rows)], today=today)
        else:  # ViewKind.GRID
            title_label.update(grid_title(self._viewed_date, self._grid_days))
            rows = grid_rows(
                index=services.index,
                calendars=calendars,
                selection=self._selection,
                viewed=self._viewed_date,
                days=self._grid_days,
            )
            # Group the flat row list back into per-day buckets the
            # widget expects.
            buckets: list[tuple[date, list[OccurrenceRow]]] = [
                (self._viewed_date + timedelta(days=offset), [])
                for offset in range(self._grid_days)
            ]
            for occ_row in rows:
                day_index = (
                    occ_row.occurrence.start.astimezone(UTC).date() - self._viewed_date
                ).days
                if 0 <= day_index < self._grid_days:
                    buckets[day_index][1].append(occ_row)
            timeline.show_days(buckets, today=today)
        self._last_rows = rows

    def _refresh_detail(self) -> None:
        component = self._currently_selected_component()
        view: EventView = self.query_one(EventView)
        today = self._services().now().date()
        view.show(component, today=today)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Refresh the detail pane when the cursor moves in the event
        # list (Agenda view). The TimelineGrid is also a DataTable,
        # but Day/Grid views hide the inline detail pane — the user
        # opens detail explicitly with Enter (handled by
        # `on_timeline_grid_selected`) — so skip the refresh when the
        # event came from the timeline.
        # `event.data_table` / `event.control` are partially-typed
        # in basedpyright (the `DataTable[Unknown]` generic param).
        # `cast` flattens it to the bare widget for the identity
        # check that decides which DataTable raised the event.
        from textual.widget import Widget

        sender = cast(Widget, event.control)
        if sender is self.query_one(TimelineGrid):
            return
        self._refresh_detail()

    def on_timeline_grid_selected(self, event: TimelineGrid.Selected) -> None:
        component = self._services().index.get_component(event.ref)
        if component is None:
            return
        self._open_specific(component)

    def _currently_selected_component(self) -> StoredComponent | None:
        # Different views surface "what's highlighted" through
        # different widgets. Agenda uses the row cursor on EventList;
        # Day / Grid use the cell cursor on TimelineGrid (and only
        # cells that hold an event resolve to a ref).
        if self._view == ViewKind.AGENDA:
            event_list: EventList = self.query_one(EventList)
            ref = event_list.selected_ref()
        else:
            timeline: TimelineGrid = self.query_one(TimelineGrid)
            coord = timeline.cursor_coordinate
            ref = timeline.cell_ref(coord.row, coord.column)
        if ref is None:
            return None
        return self._services().index.get_component(ref)

    def _services(self) -> TuiServices:
        # ChronosApp constructs MainScreen and always sets `.services`.
        # `self.app` is typed as App[Any]; cast it to our concrete
        # subclass so the attribute lookup is statically checked. We
        # import the type only under TYPE_CHECKING — `app.py` imports
        # MainScreen, so a runtime import would cycle.
        return cast("ChronosApp", self.app).services

    def _save_event(self, draft: EditDraft) -> None:
        services = self._services()
        now = services.now()
        if draft.existing is None:
            uid = generate_uid(
                draft.target.account_name,
                draft.target.calendar_name,
                draft.summary,
                draft.dtstart,
                now,
            )
            ref = ComponentRef(
                draft.target.account_name, draft.target.calendar_name, uid
            )
            ics = build_event_ics(
                uid,
                draft.summary,
                draft.dtstart,
                draft.dtend,
                now,
                location=draft.location,
                description=draft.description,
            )
            services.mirror.write(
                ResourceRef(draft.target.account_name, draft.target.calendar_name, uid),
                ics,
            )
            component = VEvent(
                ref=ref,
                href=None,
                etag=None,
                raw_ics=ics,
                summary=draft.summary,
                description=draft.description or None,
                location=draft.location or None,
                dtstart=draft.dtstart,
                dtend=draft.dtend,
                status=None,
                local_flags=frozenset(),
                server_flags=frozenset(),
                local_status=LocalStatus.ACTIVE,
                trashed_at=None,
                synced_at=None,
            )
            services.index.upsert_component(component)
            self._refresh_local_occurrences(component)
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                f"Created {draft.summary!r}"
            )
        else:
            existing = draft.existing
            ics = build_event_ics(
                existing.ref.uid,
                draft.summary,
                draft.dtstart,
                draft.dtend,
                now,
                location=draft.location,
                description=draft.description,
            )
            services.mirror.write(existing.ref.resource, ics)
            updated = VEvent(
                ref=existing.ref,
                href=existing.href,
                etag=existing.etag,
                raw_ics=ics,
                summary=draft.summary,
                description=draft.description or None,
                location=draft.location or None,
                dtstart=draft.dtstart,
                dtend=draft.dtend,
                status=existing.status,
                local_flags=existing.local_flags,
                server_flags=existing.server_flags,
                local_status=existing.local_status,
                trashed_at=existing.trashed_at,
                synced_at=existing.synced_at,
            )
            services.index.upsert_component(updated)
            self._refresh_local_occurrences(updated)
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                f"Updated {draft.summary!r}"
            )
        self.refresh_view()

    def _refresh_local_occurrences(self, component: StoredComponent) -> None:
        """Re-expand `component` and rewrite its `occurrences` rows.

        `IndexRepository.upsert_component` invalidates (deletes) the
        occurrence rows for the master so a stale cache doesn't
        outlive a content change. The sync engine repopulates the
        cache by calling `populate_occurrences`, but local create /
        edit flows have no such backstop — without a refresh here,
        the saved event vanishes from every view that joins
        `components` against `occurrences` (agenda, day, week,
        month) until the next sync. Single-component expansion is
        cheap and matches the window the sync engine uses, so the
        cache stays consistent with what `populate_occurrences`
        would have produced.
        """
        services = self._services()
        now = services.now()
        occurrences = expand(
            master=component,
            overrides=(),
            window_start=now - _LOCAL_OCCURRENCE_WINDOW_PAST,
            window_end=now + _LOCAL_OCCURRENCE_WINDOW_FUTURE,
        )
        services.index.set_occurrences(component.ref, occurrences)

    def _trash(self, component: StoredComponent) -> None:
        services = self._services()
        trashed = trashed_copy(component, trashed_at=services.now())
        services.index.upsert_component(trashed)
        self.app.notify(  # pyright: ignore[reportUnknownMemberType]
            f"Trashed {component.summary or component.ref.uid!r}"
        )
        self.refresh_view()

    def _edit_specific(self, component: StoredComponent) -> None:
        services = self._services()
        calendars = all_calendar_refs(services.config, services.mirror)
        screen = EventEditScreen(
            calendars=calendars,
            existing=component,
            default_calendar=component.ref.calendar,
            on_save=self._save_event,
        )
        self.app.push_screen(screen)  # pyright: ignore[reportUnknownMemberType]

    def _open_specific(self, component: StoredComponent) -> None:
        today = self._services().now().date()
        screen = EventDetailScreen(component, today=today, on_edit=self._edit_specific)
        self.app.push_screen(screen)  # pyright: ignore[reportUnknownMemberType]

    def _run_sync(self) -> None:
        runner = self._services().sync_runner
        if runner is None:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                "Sync from inside the TUI is not wired in this build."
            )
            return
        # Push the foreground progress dialog. It owns the worker, the
        # cancel event, and the live log tail; MainScreen just needs
        # to refresh the view once it dismisses.
        screen = SyncProgressScreen(runner, on_finished=self._sync_finished)
        self.app.push_screen(screen)  # pyright: ignore[reportUnknownMemberType]

    def _sync_finished(
        self,
        results: Sequence[SyncResult],
        error: BaseException | None,
    ) -> None:
        del results, error  # the dialog already showed the summary
        self.refresh_view()

    def _first_selected(self, calendars: tuple[CalendarRef, ...]) -> CalendarRef:
        for ref in calendars:
            if self._selection.contains(ref):
                return ref
        return calendars[0]


def _save_last_view(view: ViewKind) -> None:
    import contextlib

    with contextlib.suppress(OSError):
        default_tui_state_path().write_text(view.value, encoding="utf-8")


def _load_last_view() -> ViewKind:
    try:
        text = default_tui_state_path().read_text(encoding="utf-8").strip()
        return ViewKind(text)
    except (OSError, ValueError):
        return ViewKind.AGENDA


__all__ = ["MainScreen"]
