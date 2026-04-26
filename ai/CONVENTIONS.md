# CONVENTIONS.md

Engineering rules. Language, tooling, typing, testing, style, dependencies, configuration, versioning, build, TUI.

## 1. Language and runtime

- **Python 3.13** — minimum. Pin is `.python-version`.
- **uv** — for dependency management and running everything (`uv sync`, `uv run`, `uv add`).
- **hatchling** — build backend, declared in `pyproject.toml`.

## 2. Quality gates

All must pass on every change. No exceptions, no `--no-verify`.

```
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/
uv run basedpyright src/
uv run python -m pytest tests/
```

Failures are root-caused, not papered over with ignores.

## 3. Typing

- `mypy --strict` clean. `basedpyright` strict clean.
- Prefer `Protocol` classes over abstract base classes for repositories and services.
- Use frozen `@dataclass` for domain objects. Mutation happens by replacement, not by assignment.
- Textual's `Screen.app` is generic; when a public Textual call raises `reportUnknownMemberType`, suppress it narrowly on the public call only (`self.app.push_screen(...)  # pyright: ignore[reportUnknownMemberType]`). Never suppress on private calls.
- No bare `# type: ignore` — always cite a specific diagnostic code.

## 4. Testing

- `unittest.TestCase` classes, run via `pytest`. Don't mix in pytest-style fixtures unless there's a specific reason.
- Sync tests use `FakeCalDAVSession`, a deterministic in-memory test double conforming to the `CalDAVSession` protocol. Tests never hit the network.
- Storage tests run a conformance suite against every `MirrorRepository` implementation.
- Index tests use real `SqliteIndexRepository` against a temp-dir database. No mocking.
- TUI tests use Textual's `Pilot`, with `pytest-asyncio` in `asyncio_mode = "auto"`. The flow plan lives in `ai/TUI_TESTING_PLAN.md`; the implementation is `tests/test_tui_flows.py`.
- **Coverage** is measured with `pytest-cov` (branch). The default test command is `uv run python -m pytest --cov=chronos --cov-branch --cov-fail-under=85 tests/`. See `AGENTS.md §5` for the full policy: the floor is a ratchet, new code lands at 100%, exclusions are narrow / named / justified.
- Corpus lives in `tests/corpus.py`. All test addresses, organisers, and attendees use `@example.com`. All test dates anchor to fixed 2026 reference dates so recurrence assertions are reproducible.

## 5. Test corpus

At minimum, `tests/corpus.py` exposes:

- `simple_event()` — a single timed VEVENT.
- `timed_event_with_tz()` — VEVENT with embedded VTIMEZONE.
- `all_day_event()` — DATE-only DTSTART/DTEND.
- `recurring_weekly()` — RRULE;FREQ=WEEKLY.
- `recurring_with_exceptions()` — RRULE plus EXDATE plus one RECURRENCE-ID override.
- `recurring_count()` — RRULE;COUNT=N.
- `recurring_until()` — RRULE;UNTIL=...
- `simple_todo()` — VTODO with DUE.
- `completed_todo()` — VTODO with COMPLETED and STATUS:COMPLETED.
- `malformed_missing_uid()` — triggers the synthetic-UID path (`SYNCHRONIZATION.md §C-8`).
- `duplicate_uid()` — second one gets a synthetic UID (§C-7).

## 6. Code style

- **ruff** rules: `E, F, I, B, UP, N, ARG, SIM`. Line length **88**. `ruff format` canonical.
- Imports sorted by ruff (isort-compatible).
- No emojis anywhere: code, comments, commits, docs.
- No docstrings on obvious methods. Comments only where logic isn't self-evident — explain *why*, not *what*.
- No error handling for scenarios that can't happen. Trust internal code; validate only at system boundaries (user input, network, filesystem at entry points).
- Prefer editing an existing file to creating a new one. Splits happen when a file becomes genuinely hard to work with, not pre-emptively.

## 7. Approved runtime dependencies

Only these. Anything else needs explicit approval before `uv add`.

| Package | Purpose |
|---|---|
| `caldav` | CalDAV client (PROPFIND, REPORT sync-collection / calendar-query / calendar-multiget, PUT/DELETE with `If-Match`). |
| `icalendar` | RFC 5545 parse and serialize. |
| `python-dateutil` | RRULE expansion, timezone-aware datetime arithmetic. |
| `textual` | Terminal UI framework. |
| `mcp` | Model Context Protocol server SDK. |
| `tomli-w` | Writing `config.toml` from the config-editing CLI. Python 3.13 has `tomllib` for reading only. |

OAuth 2.0 support (Google, Microsoft) was evaluated against `google-auth`/`google-auth-oauthlib` but neither is added — `google-auth` requires `requests`, which we don't ship. The OAuth code in `chronos.oauth` uses `niquests` (already a transitive dep via `caldav`) directly.

**Note on `caldav` vs raw `httpx` + XML.** Default to `caldav` for v1. Revisit only if it blocks a specific reliability property (idempotent PUT with `If-Match` semantics, conditional REPORT behaviour) that we cannot get through it.

**`anyio`** is a transitive dependency via `mcp` and is therefore available without an explicit `uv add`. It may be used in transport code (`mcp_transport.py`) for task groups and memory streams. No uvicorn, starlette, fastapi, h11, or httpx for the server side — standard library `asyncio` plus `anyio` only.

## 8. Configuration

- A single TOML file is the only configuration format.
- Parsed directly into domain dataclasses; no intermediate DTO layer.
- `config-sample.toml` lives in the repo root and stays synchronised with `config.py`. If a field is added, removed, or renamed, update the sample in the same change.
- Path values (`mirror_path`, credential-backend paths, etc.) support `~`, `$VAR`, and `%VAR%` expansion.
- All sample and test addresses use `@example.com`.

## 9. Version management

- Version string lives in two files and is kept in sync atomically:
  - `pyproject.toml` — `project.version`.
  - `src/chronos/version.py` — `__version__`.
- `CHANGELOG.md` (Keep a Changelog format) is the source of truth.
- The release workflow is manually dispatched. It reads the top undated heading `## [X.Y.Z]` in `CHANGELOG.md`, writes that version into both files, stamps the date, tags, and publishes.
- Never edit either file's version manually.

## 10. Build process

- Chain: `docs/` (MkDocs source) → `site/` (HTML) → `chronos.spec` (PyInstaller) → `dist/chronos/` → archives + platform installers.
- `site/` is generated; gitignored; never committed.
- `chronos.spec` declares bundled data via its `datas` list.
- `paths.bundled_docs_path()` detects PyInstaller frozen execution and resolves to the bundled docs directory.
- Platform installers: Inno Setup (Windows), `hdiutil` (macOS DMG), `appimagetool` (Linux AppImage).

All of §10 is Milestone 8 material — it exists as specification here, not yet as code.

## 11. TUI conventions

- Each screen owns its `BINDINGS` list. No global mail-of-bindings.
- The footer displays the current screen's bindings only.
- Screens use public Textual API exclusively: `self.app.push_screen(...)`, `self.app.notify(...)`, `self.app.pop_screen()`. Never `self.app._private_method()`.
- The sync workflow lives in `main_screen.py`, not `app.py`. `SyncConfirmScreen` receives an `on_confirm` callback; it does not hold a reference to the `ChronosApp`.
- Views are read-only projections of `IndexRepository`. Mutations go through explicit user actions that call repository methods, not through view side effects.
