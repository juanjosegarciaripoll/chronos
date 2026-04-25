# TASKS.md

Actionable backlog. Top of file is next up; bottom is later. Each milestone is a coherent chunk of work that can be shipped and tested on its own.

## Current state

Milestones 0–10 shipped. `chronos tui` opens the Textual UI against the same mirror + index the CLI uses; day / week / month / agenda / todo views, create / edit / trash flows, sync confirm, and search are wired end-to-end. Sync against real Google + CSIC accounts works; per-calendar / per-batch / per-ingest progress is visible at INFO. Crash safety is in place (atomic writes everywhere, sync resumability, 412 lost-response recovery, sync.lock against concurrent runs). The read-only MCP server is wired through `chronos mcp` (stdio transport) with five tools: `list_calendars`, `query_range`, `search`, `get_event`, `get_todo`. Next up is **Milestone 11 — Packaging and release**.

## Milestone 0 — Project scaffolding

- Add runtime deps to `pyproject.toml`: `caldav`, `icalendar`, `python-dateutil`, `textual`, `mcp`.
- Add dev group: `ruff`, `mypy`, `basedpyright`, `pytest`, `pytest-asyncio`, `pyinstaller`, `mkdocs-material`, `pyinstrument`.
- Configure ruff (rules `E, F, I, B, UP, N, ARG, SIM`; line length 88), mypy strict, basedpyright strict, pytest (`asyncio_mode = "auto"`) in `pyproject.toml`.
- Rename `main.py` → `src/chronos/__main__.py`; add `src/chronos/__init__.py` and an empty `src/chronos/version.py` with `__version__ = "0.1.0"`.
- Create empty module stubs for every file listed in `ARCHITECTURE.md §1`.
- Add `config-sample.toml` at repo root with one commented-out CalDAV account block.
- Initialise `CHANGELOG.md` with a `## [Unreleased]` heading.

**Acceptance:** `uv sync` succeeds; all five quality gates pass on an empty codebase; `python -m chronos` runs without error.

## Milestone 1 — Domain + config

- `domain.py` — frozen dataclasses: `AccountConfig`, `CalendarConfig`, `CredentialSpec`, `CalendarRef`, `ComponentRef`, `VEvent`, `VTodo`, `Occurrence`, plus enums (`ComponentKind`, `LocalStatus`).
- `protocols.py` — Protocols for `CalDAVSession`, `MirrorRepository`, `IndexRepository`, `CredentialsProvider`, `SyncService`.
- `config.py` — TOML parsing with path expansion (`~`, `$VAR`, `%VAR%`), validation errors pointed at the offending key.
- `paths.py` — XDG/Windows directory resolution, `bundled_docs_path()`.
- `tests/corpus.py` — bootstrap with the fixtures listed in `CONVENTIONS.md §5`.
- Unit tests for config parsing (valid, missing, invalid) and path expansion.

**Acceptance:** domain, protocols, config, paths implemented with full type coverage; corpus provides every fixture listed; config tests green.

## Milestone 2 — Mirror + index

- `storage.py` — vdir-style `.ics` mirror with a conformance test suite. Crash-safe writes via temp-file + rename.
- `index_store.py` — SQLite schema (`components`, `occurrences`, `calendar_sync_state`), FTS5 virtual table, `connection()` context manager, narrow-projection helpers.
- `storage_indexing.py` — mirror → index projection pipeline.
- `ical_parser.py` — thin wrapper over `icalendar` for parse/serialize with our domain types.
- Conformance suite against the mirror; real-SQLite tests against the index.

**Acceptance:** round-trip of every corpus fixture through mirror + index; FTS queries return hits; `storage_indexing` is idempotent.

## Milestone 3 — Recurrence

- `recurrence.py` — `expand(master, overrides, window_start, window_end)` per `RECURRENCE.md §2`.
- `occurrences` cache management: invalidation on master/override writes; lazy repopulation by views.
- Tests for every edge case in `RECURRENCE.md §5`.
- Bench one "infinite RRULE in a 25-month window" case to confirm no unbounded expansion.

**Acceptance:** expansion tests green; cache invalidation covered; no infinite-loop escape.

## Milestone 4 — CalDAV sync

- `caldav_client.py` — `CalDAVSession` implementation wrapping `caldav`: `discover_principal`, `list_calendars`, `get_ctag`, `sync_collection`, `calendar_query`, `calendar_multiget`, `put`, `delete`, `move`.
- `sync.py` — two-phase engine (plan / execute), CTag-gated path selection, per-calendar reconciliation (§7), push ordering.
- `FakeCalDAVSession` test double — deterministic, in-memory, implements the full Protocol.
- Sync tests covering C-1 through C-11 plus the three paths (fast / medium / slow).
- Integration tests against a Radicale instance (optional; behind a `CHRONOS_INTEGRATION=1` env guard).

**Acceptance:** all conflict scenarios covered; fast path is zero-I/O beyond CTag; idempotency verified by mid-run-abort tests.

## Milestone 5 — CLI + doctor

- `cli.py` — `chronos sync`, `list`, `show`, `add`, `edit`, `rm`, `doctor`.
- `services.py` — doctor diagnostics: credentials ping (PROPFIND against principal), mirror integrity (bytes ↔ index), occurrence cache staleness.
- `credentials.py` — four backends (plaintext, env, command, encrypted keyring).
- CLI tests using a captured-stdout harness.

**Acceptance:** every command usable offline against a seeded mirror+index; doctor reports real issues on a deliberately-corrupted fixture.

## Milestone 6 — End-to-end CLI usability

Reprioritised from the original "TUI" milestone. Before the TUI makes sense, a user has to be able to configure chronos and sync against a real server from the command line.

**Config-editing CLI** (via `tomli-w`, now approved in `CONVENTIONS.md §7`):

- `chronos init` — write a minimal `config.toml` at the default path (if missing).
- `chronos account add --name ... --url ... --username ... --credential-backend {plaintext|env|command} --credential-value ... --mirror-path ...` — append an account.
- `chronos account list` — show configured accounts; never prints passwords.
- `chronos account rm NAME` — remove by name.
- `chronos config edit` — open `config.toml` in `$EDITOR`; on save, reparse + validate; offer to re-edit or discard on validation failure.
- `config.dump()` / `config.save()` helpers round-trip `AppConfig` through TOML.

**Real CalDAV HTTP client** — replace every `NotImplementedError` in `caldav_client.py` with a call into the `caldav` library:

- `discover_principal`, `list_calendars`, `get_ctag`, `calendar_query`, `calendar_multiget`, `put`, `delete`.
- Translate `caldav.lib.error` exceptions into the `CalDAVError` hierarchy.
- Integration tests guarded by `CHRONOS_INTEGRATION=1` env var (hit a local Radicale/Baikal; skipped by default).

**Acceptance:** `chronos init && chronos account add ... && chronos sync && chronos list` works end-to-end against a real CalDAV server with no hand-editing of `config.toml`.

## Milestone 7 — OAuth 2.0 for Google and Microsoft

Reprioritised from the original "TUI" slot. Google and Microsoft dropped basic-auth support for CalDAV; without OAuth, chronos can't talk to the two largest calendar providers.

- `src/chronos/oauth.py`: device flow (`request_device_code` / `poll_for_tokens`), token store (`save_tokens` / `load_tokens` under `paths.oauth_token_dir()`), bearer-token HTTP auth (`BearerTokenAuth` subclass of `niquests.auth.AuthBase`) with automatic refresh-grant on expiry.
- `src/chronos/authorization.py`: `Authorization` carrying either basic (username, password) or `http_auth` (AuthBase) plus an `on_commit` callback for token rotation.
- `src/chronos/domain.py`: `OAuthCredential` (client_id, client_secret, scope, optional token_path) added to the `CredentialSpec` union.
- `src/chronos/credentials.py`: `build_auth(account)` returns `Authorization`; OAuth accounts wire through `oauth.build_bearer_auth`.
- `src/chronos/caldav_client.py`: `CalDAVHttpSession` accepts `Authorization`; basic goes via `DAVClient(username, password)`, bearer goes via `DAVClient(auth=...)` (niquests AuthBase).
- `src/chronos/cli.py`: `account add --credential-backend oauth --oauth-client-id ... --oauth-client-secret ... --oauth-scope ...`; new `chronos oauth authorize --account NAME` runs the device flow.

**Not depended on:** `google-auth` — its transport layer requires `requests`, which we don't otherwise ship. Refresh grant is ~40 lines of straightforward HTTP against `niquests` (transitive via `caldav`).

**Acceptance:** a user who has created a Google Cloud OAuth client can `chronos account add --credential-backend oauth`, `chronos oauth authorize`, `chronos sync` against Google Calendar with no hand-editing of `config.toml`.

## Milestone 8 — TUI (shipped)

- `tui/app.py` (ChronosApp + TuiServices), `tui/bindings.py` (per-screen builders), `tui/views.py` (pure projection helpers), and the screen + widget files in `ARCHITECTURE.md §1`.
- Day, week, month, agenda, todo-list views — keys `d / w / m / a / t`, with `T` returning to today.
- Three-pane layout: calendar tree, view list, detail pane. Screen-owned bindings; footer shows current screen only (`CONVENTIONS.md §11`).
- Mutating flows: `n` new, `e` edit, `x` trash (via `ConfirmScreen`), `s` sync (via `SyncConfirmScreen` + injected `sync_runner`), `/` search.
- Shared write helpers extracted to `mutations.py` so CLI and TUI use the same `build_event_ics` / `generate_uid` / `trashed_copy`.
- `chronos tui` CLI command wires the app to a real `TuiServices`.
- `ai/TUI_TESTING_PLAN.md` documents the two-layer test approach. `tests/test_tui_flows.py` exercises the pure helpers (Layer 1) and drives `ChronosApp` headlessly via `App.run_test()` / `Pilot` for the eight named flows (Layer 2).

**Acceptance:** all five views navigable; create / edit / trash flows work end-to-end against a seeded repo; TUI tests green; project-wide branch coverage ≥ 88%.

## Milestone 9 — Crash safety

Audit and harden every persistence path so an interrupt (Ctrl-C / SIGINT, terminal close, OS reboot, OOM kill) at any point during sync leaves chronos in a coherent on-disk state, and the next run resumes correctly. Sync against a real Google or Nextcloud account already takes minutes for big calendars; users will Ctrl-C, and the v1 promise is that doing so is safe.

**Atomicity audit** (mostly verification — most paths are already correct):

- `storage.VdirMirrorRepository.write` / `move` / `delete` — confirm every write is temp-file + `os.replace` + chmod, never an in-place truncate. Add a conformance test that asserts no `*.tmp` file is left behind after a successful write, and that a simulated crash mid-write (raise inside the temp-file context) leaves either the prior file intact or no file at all — never a half-written one.
- `index_store.SqliteIndexRepository.connection()` — confirm every multi-row update (`_ingest_resource`'s component upserts, `populate_occurrences`'s per-master expansion, `_apply_server_deletions`) goes through the context manager so an interrupt either commits the whole batch or rolls it back. Add a test that raises `KeyboardInterrupt` inside the `connection()` block and asserts no partial rows survive.
- `oauth.save_tokens` — already temp-file + `os.replace`; add a leftover-tmp test.
- `config.save` (used by `chronos account add` / `config edit`) — same audit + test.

**Sync resumability:**

- Document the load-bearing invariant in `sync.py`: CTag + sync-token are written to `calendar_sync_state` only after `_sync_calendar` returns successfully. Mid-sync interrupts leave the prior CTag in place, so the next run re-enters the slow path and reconverges. Add a test that runs `_sync_calendar` against a `FakeCalDAVSession` that raises mid-batch, then runs it again and asserts the same end state as an uninterrupted run.
- Push paths (`_push_pending`, `_push_trashed`): if PUT succeeded server-side but the response was lost, the local row stays at `href IS NULL` and re-pushing returns 412 (`If-None-Match: *` against the now-existing resource). Plan: on 412 from a new-resource PUT, do a calendar-query lookup, match by content hash, adopt the existing href + etag. Without this, a single dropped response can wedge a row in a retry loop.

**Process-level guard:**

- Add a lockfile at `paths.user_data_dir() / "sync.lock"` acquired by `cmd_sync` (and `build_sync_runner` for the TUI) and released on exit. Concurrent `chronos sync` invocations fail loudly with the holder's PID. Use `fcntl.flock` on POSIX and `msvcrt.locking` on Windows; detect and replace stale locks (holder PID dead).
- The OAuth loopback flow's `HTTPServer` already has a `try/finally` that calls `server_close`; add an explicit test that Ctrl-C during the wait releases the port.

**TUI:**

- The TUI runs sync on a worker thread. Verify Ctrl-C / app-quit during sync neither corrupts state nor leaves Textual in a half-rendered screen. If the worker can't be interrupted cleanly mid-multiget, document that as a known limitation.

**Acceptance:** every persistence write is atomic by inspection or by test; an interrupted-then-resumed sync reaches the same end state as an uninterrupted one (proven by a fault-injection test); concurrent `chronos sync` invocations are rejected; project-wide branch coverage ≥ 88% holds.

## Milestone 10 — MCP server

- `mcp_server.py` — read-only tools: `list_calendars`, `query_range(start, end)`, `search(query)`, `get_event(uid)`, `get_todo(uid)`.
- MCP tests that stand up a server in-process and exercise each tool.

**Acceptance:** MCP server starts cleanly; each tool returns expected payloads against a seeded index; no write tools present.

## Milestone 11 — Packaging and release

- `chronos.spec` — PyInstaller spec.
- `scripts/build.py` — orchestrates tests + docs + binary + archive + installer.
- `docs/` — MkDocs Material site mirroring pony's structure.
- `CHANGELOG.md` — first real entry.
- GitHub Actions release workflow (manually dispatched), mirroring pony's.

**Acceptance:** `uv run python scripts/build.py` produces a bundle that launches on the target platform; release workflow dry-runs cleanly.

## Followups / open questions

- **Keyring-backed OAuth token storage** — M7 writes refresh tokens as plain JSON under `paths.oauth_token_dir()` with a best-effort 0600 chmod on POSIX (no-op on Windows). When the `keyring` dep is approved, migrate tokens to the system keyring for defence-in-depth.
- **Conditional DELETE with If-Match** — caldav 3.1 doesn't expose headers on `DAVClient.delete()`. Not a correctness issue (sync engine's etag reconciliation catches server-side races on next pass) but revisit when caldav grows the API.
- **iTIP / iMIP** — meeting requests and RSVPs. Needs an SMTP send path; touches `caldav_client` (schedule-outbox) and compose flows. Deferred (`SPECIFICATIONS.md §4`).
- **Free/busy** — CalDAV `free-busy-query` REPORT. Deferred.
- **OAuth** — Google/Microsoft token flows. Deferred; revisit when a user demand case lands.
- **Browser UI** — deferred.
- **Background sync daemon** — deferred; v1 is explicit sync only.
- **THISANDFUTURE overrides** — currently treated as single-instance overrides (`RECURRENCE.md §5`). Revisit if recurrence editing proves clumsy.
- **Multi-calendar server links** — Google-style duplicated resources across calendars. Deferred.
- **Write contention** — SQLite `busy_timeout` only, no exclusive sync lock. Reassess if the TUI races the sync engine in practice.
- **Per-account `mirror_path` is not honored at runtime** — `AccountConfig.mirror_path` is parsed from `config.toml` (or defaulted to `paths.default_mirror_path(name)`) and stored on the dataclass, but the CLI / TUI build a single `VdirMirrorRepository(default_mirror_dir())` and ignore the per-account value. As long as users take the default the on-disk layout coincides; custom values silently fall back. Either route through a per-account mirror map or drop the field; not blocking M9.
- **TUI in-app OAuth modal** — CLI sync now runs the OAuth loopback flow inline whenever an account has no token file (see `cli._default_cli_authorizer`, which delegates to `oauth.run_loopback_flow`: opens the browser, captures the redirect on a random local port, exchanges the code via PKCE). The TUI can't open a browser inline cleanly, so `cli._tui_unsupported_authorizer` surfaces a "quit and run `chronos sync` once" message. The cleaner UX is a Textual modal that triggers the loopback flow from a worker thread and polls token state, so the user stays inside the TUI on first run / re-auth.
- **OAuth device-flow opt-in** — `oauth.run_loopback_flow` is the default (RFC 8252 + PKCE, "Desktop app" OAuth client type, browser required). The device flow (RFC 8628, "TVs and Limited Input devices" client type) is still implemented (`oauth.request_device_code` + `poll_for_tokens`) and exposed via `cli._default_device_flow`, but there's no CLI surface to opt into it yet. Add `chronos oauth authorize --device` (and a config-level fallback) for SSH / headless users without a local browser.
- **OAuth refresh-failure auto re-auth** — token *expiry while authorized* refreshes automatically inside `BearerTokenAuth.__call__`. If the refresh grant itself fails (revoked refresh token, scope changed, etc.), the error currently bubbles up through niquests as a request failure. The provider should detect the refresh-failure case and re-run the loopback flow, mirroring the missing-tokens path. Needs a way to surface the error from inside the auth callable back to the credentials provider.
- **Hybrid occurrence expansion** — `sync_account` currently calls `populate_occurrences` with a wide static window (`now - 30y` to `now + 5y`) per calendar. For typical calendars (≤100 masters, mostly weekly/monthly recurrences) this writes a few thousand rows total — fine. For a daily-forever event over 35 years it hits `MAX_OCCURRENCES=10_000` and the master is silently skipped (no rows for it; invisible in the TUI). The right long-term fix is per-master smart windowing: non-recurring masters get a 1-row cache regardless of window (window-independent), recurring masters cache a narrow window like `[today - 1y, today + 2y]`, and `views.gather_occurrences` falls back to on-demand `expand()` when a query window extends beyond the cached range. This keeps the cache small while preserving SQL-fast common-case queries. Out of scope for v1; revisit when a heavy calendar surfaces problems.
- **TUI sync on a Textual worker** — `MainScreen._run_sync` currently calls the sync_runner synchronously on the UI thread, so the TUI is frozen for the duration and a Ctrl-C exits the app rather than just cancelling the sync. The persistence layers all release cleanly under interrupt (lockfile contextmanager, SQLite per-resource transactions, atomic mirror writes), so the on-disk state is consistent — but the UX would be better if sync ran on a Textual worker (`@work`) with a status banner and a "cancel sync" key. Out of scope for the crash-safety milestone (correctness done); revisit when polishing the TUI.
- **Pipelined fetch + ingest** — _Shipped._ `sync._fetch_and_ingest` runs the network fetch on a daemon producer thread and the parse + mirror-write + index-upsert on the main consumer thread, drained through a bounded queue (`_FETCH_PIPELINE_BUFFER = 2` chunks of `_FETCH_CHUNK_SIZE = 100` hrefs each). SQLite stays single-writer because only the consumer touches the index; the producer talks to one `CalDAVHttpSession` from one thread, so niquests' non-thread-safety is moot. Producer-side exceptions funnel through the queue and re-raise on the consumer; the consumer's `finally` flips a cancel event and `join`s the producer so a crashed sync can't leak threads. Verified by `tests/test_sync.py::FetchIngestPipelineTest` (deterministic overlap test deadlocks if the second multiget waits on the first ingest to finish).
