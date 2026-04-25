from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
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
from chronos.tui.screens.help_screen import HelpScreen
from chronos.tui.screens.month_view_screen import (
    rows_for as month_rows,
)
from chronos.tui.screens.month_view_screen import (
    title_for as month_title,
)
from chronos.tui.screens.search_dialog_screen import SearchDialogScreen
from chronos.tui.screens.sync_confirm_screen import SyncConfirmScreen
from chronos.tui.screens.sync_progress_screen import SyncProgressScreen
from chronos.tui.screens.todo_list_screen import (
    rows_for as todo_rows,
)
from chronos.tui.screens.todo_list_screen import (
    title_for as todo_title,
)
from chronos.tui.screens.week_view_screen import (
    rows_for as week_rows,
)
from chronos.tui.screens.week_view_screen import (
    title_for as week_title,
)
from chronos.tui.views import (
    CalendarSelection,
    OccurrenceRow,
    ViewKind,
    all_calendar_refs,
)
from chronos.tui.widgets.calendar_panel import CalendarPanel
from chronos.tui.widgets.event_list import EventList
from chronos.tui.widgets.event_view import EventView

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
        self._viewed_date: date = date(2026, 4, 25)  # rebound in on_mount
        self._selection = CalendarSelection(refs=frozenset())
        self._last_rows: tuple[OccurrenceRow, ...] = ()

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-body"):
            yield CalendarPanel()
            with Vertical(id="centre-pane"):
                yield Label("", id="view-title")
                yield EventList(id="centre-list")
                yield EventView(id="detail-pane")
        yield Footer()

    def on_mount(self) -> None:
        services = self._services()
        self._viewed_date = services.now().date()
        panel: CalendarPanel = self.query_one(CalendarPanel)
        panel.populate(all_calendar_refs(services.config, services.mirror))
        self.refresh_view()
        # Land focus on the event list, not the calendar tree, so the
        # user can navigate / open / edit events without first having
        # to tab out of the left-hand panel.
        self.query_one(EventList).focus()

    # View switches ----------------------------------------------------------

    def action_view_day(self) -> None:
        self._view = ViewKind.DAY
        self.refresh_view()

    def action_view_week(self) -> None:
        self._view = ViewKind.WEEK
        self.refresh_view()

    def action_view_month(self) -> None:
        self._view = ViewKind.MONTH
        self.refresh_view()

    def action_view_agenda(self) -> None:
        self._view = ViewKind.AGENDA
        self.refresh_view()

    def action_view_todos(self) -> None:
        self._view = ViewKind.TODOS
        self.refresh_view()

    def action_today(self) -> None:
        # Always do something visible: snap viewed_date to now AND, if
        # we're on a view that ignores viewed_date (agenda / todos),
        # switch to the day view so the user actually sees "today".
        self._viewed_date = self._services().now().date()
        if self._view in (ViewKind.AGENDA, ViewKind.TODOS):
            self._view = ViewKind.DAY
        self.refresh_view()

    def action_next_period(self) -> None:
        self._step_period(direction=+1)

    def action_prev_period(self) -> None:
        self._step_period(direction=-1)

    def _step_period(self, *, direction: int) -> None:
        # Advance / retreat the viewed date by the natural unit for
        # the current view. Agenda + todos ignore `_viewed_date`, so
        # `N` / `P` are no-ops there. Day view also doesn't act here
        # — the user can hop ±1 day with `t` (today) and the date
        # picker; binding day to N/P would clash with the user's
        # explicit "week or month" framing.
        if self._view == ViewKind.WEEK:
            self._viewed_date = self._viewed_date + timedelta(days=7 * direction)
        elif self._view == ViewKind.MONTH:
            self._viewed_date = self._viewed_date + relativedelta(months=direction)
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
        # Friendly date labels (Today / Tomorrow / weekday) are anchored
        # on the user's actual today, not on the viewed date — looking
        # at a 2014 day still shows the absolute date, not "Today".
        now = services.now()
        today = now.date()
        if self._view == ViewKind.DAY:
            title_label.update(day_title(self._viewed_date))
            rows = day_rows(
                index=services.index,
                calendars=calendars,
                selection=self._selection,
                viewed=self._viewed_date,
            )
            self._last_rows = rows
            event_list.show_events(rows, today=today, now=now)
        elif self._view == ViewKind.WEEK:
            title_label.update(week_title(self._viewed_date))
            rows = week_rows(
                index=services.index,
                calendars=calendars,
                selection=self._selection,
                viewed=self._viewed_date,
            )
            self._last_rows = rows
            event_list.show_events(rows, today=today, now=now)
        elif self._view == ViewKind.MONTH:
            title_label.update(month_title(self._viewed_date))
            rows = month_rows(
                index=services.index,
                calendars=calendars,
                selection=self._selection,
                viewed=self._viewed_date,
            )
            self._last_rows = rows
            event_list.show_events(rows, today=today, now=now)
        elif self._view == ViewKind.AGENDA:
            title_label.update(agenda_title(today))
            rows = agenda_rows(
                index=services.index,
                calendars=calendars,
                selection=self._selection,
                today=today,
            )
            self._last_rows = rows
            event_list.show_events(rows, today=today, now=now)
        else:  # TODOS
            title_label.update(todo_title())
            self._last_rows = ()
            todos = todo_rows(
                index=services.index,
                calendars=calendars,
                selection=self._selection,
            )
            event_list.show_todos(todos)
        self._refresh_detail()

    def _refresh_detail(self) -> None:
        component = self._currently_selected_component()
        view: EventView = self.query_one(EventView)
        today = self._services().now().date()
        view.show(component, today=today)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Refresh the detail pane whenever the cursor moves in the
        # event list. Without this, arrow-key navigation through the
        # list left the detail pane stuck on the row that was current
        # at the last `refresh_view`. There's only one DataTable on
        # this screen (`EventList`), so we don't need to filter on
        # `event.data_table`.
        del event
        self._refresh_detail()

    def _currently_selected_component(self) -> StoredComponent | None:
        event_list: EventList = self.query_one(EventList)
        ref = event_list.selected_ref()
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
            ics = build_event_ics(uid, draft.summary, draft.dtstart, draft.dtend, now)
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
                description=None,
                location=None,
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
                existing.ref.uid, draft.summary, draft.dtstart, draft.dtend, now
            )
            services.mirror.write(existing.ref.resource, ics)
            updated = VEvent(
                ref=existing.ref,
                href=existing.href,
                etag=existing.etag,
                raw_ics=ics,
                summary=draft.summary,
                description=existing.description,
                location=existing.location,
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


__all__ = ["MainScreen"]
