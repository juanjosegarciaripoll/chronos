# TUI_TESTING_PLAN.md

How `chronos`'s Textual UI is tested.

## 1. Layers

The TUI is structured to keep most logic out of the screen classes, so tests can run in two layers:

1. **Pure helpers** — `tui/views.py`, `tui/widgets/date_picker.py`, the `*_view_screen.py` `rows_for` / `title_for` / `window_for` helpers, and `tui/mutations.py` (shared with the CLI). These are plain functions and dataclasses; tests call them directly without spinning up Textual.

2. **Pilot flows** — `tests/test_tui_flows.py` drives `ChronosApp` headlessly via `App.run_test()`, which yields a `textual.pilot.Pilot`. Tests press keys, assert on screen / widget state, and verify mutations land in the real `MirrorRepository` + `SqliteIndexRepository`.

Layer 1 carries most of the assertions. Layer 2 verifies that the bindings, screen pushes, and `notify` plumbing wire those helpers up correctly.

## 2. Fixtures

- `tests/corpus.py` — pre-existing iCalendar fixtures.
- `tests/test_tui_flows.py:_seed_workspace()` — builds a temp `mirror/` and `index.sqlite`, ingests two calendars (`work`, `personal`) under one account from corpus fixtures, populates occurrences, and returns a `TuiServices` ready to inject.
- A frozen clock: `now=lambda: datetime(2026, 4, 25, 9, 0, tzinfo=UTC)`. All date-window assertions anchor to this.

## 3. Layer-1 unit tests (in `tests/test_tui_flows.py`)

`unittest.TestCase` classes, no Pilot. Cover:

- `views.day_window`, `week_window`, `month_window`, `agenda_window` — boundary arithmetic on month rollover, week start (Monday), DST edges (none in 2026 fixtures, asserted as a known limitation).
- `views.gather_occurrences` — selection filter, trashed-component filter, sort order.
- `views.gather_todos` — only ACTIVE VTODOs, sorted by due date.
- `views.search_components` — substring match across summary/description/location, case-insensitive, empty-needle = empty result.
- `views.render_event_detail` — VEvent vs VTodo formatting; missing fields skipped.
- `widgets.date_picker.parse_date_input` — naive datetime is interpreted in the system local timezone (so the editor's input matches what the calendar views display), malformed input raises `InvalidDateError`.
- `widgets.event_list.EventList._row_key` — round-trips through stored ref+recurrence_id.
- `screens.*.title_for` — month names, week range formatting.

## 4. Layer-2 Pilot flows (in `tests/test_tui_flows.py`)

`unittest.IsolatedAsyncioTestCase` so we can `await App.run_test()`. Each test owns its temp dir, builds a fresh `TuiServices`, and runs one flow.

Required flows:

- **F1 — Five views navigable.** Press `d`/`w`/`m`/`a`/`t` in turn; assert `MainScreen._view` becomes `ViewKind.DAY` … `ViewKind.TODOS`. Assert `#view-title` text starts with the expected label.
- **F2 — Today resets viewed_date.** Press `m` → page back via direct `_viewed_date` mutation → press `T`; assert `_viewed_date == services.now().date()`.
- **F3 — New event flow end-to-end.** Press `n`; the `EventEditScreen` is on top. Type into the summary input, set the date input, press `ctrl+s`; assert the new VEVENT is in the index and on disk under `mirror/<account>/<calendar>/<uid>.ics`.
- **F4 — Edit existing event.** Seed one event, navigate to agenda, focus its row, press `e`; the edit screen pre-fills with the existing summary. Change the summary, save; assert the index row's summary is updated and the mirror file's bytes contain the new SUMMARY.
- **F5 — Trash with confirmation.** Press `x`, then `y` on the confirm screen; assert the component's `local_status` is now `LocalStatus.TRASHED` and it disappears from the next agenda gather.
- **F6 — Trash cancel.** Press `x`, then `n`; assert nothing changed.
- **F7 — Sync confirm runs the runner.** Inject a `sync_runner` that returns one fake `SyncResult`; press `s`, then `y`; assert the runner was called once.
- **F8 — Search dialog opens and selects.** Press `/`, type a substring of a seeded summary, press `enter`; assert the `EventDetailScreen` for that component is on top.

## 5. What is *not* tested at the TUI layer

- Pixel-perfect rendering. We assert on widget state, not screenshots.
- Textual's own bindings dispatch — we trust upstream.
- Sync engine behaviour — covered by `tests/test_sync.py`. The TUI test injects a stub `sync_runner` and only asserts the wiring.
- Real CalDAV HTTP — covered by `tests/test_caldav_integration.py` (CHRONOS_INTEGRATION=1, default-skipped, excluded from coverage).

## 6. Coverage targets

The TUI as a whole should land at ≥85% branch coverage at minimum (the project-wide floor in `pyproject.toml`). The pure-helper modules should be 100%; flow tests bring the screen classes up.

`MainScreen.action_sync` has a no-op branch when `services.sync_runner is None`; it is exercised by F7's "no runner" variant. The "sync from inside the TUI is not wired" notification path is the only branch in `MainScreen` that is not covered by a real-runner flow.
