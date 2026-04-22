# TASKS.md

Actionable backlog. Top of file is next up; bottom is later. Each milestone is a coherent chunk of work that can be shipped and tested on its own.

## Current state

Milestones 0–7 shipped. `chronos init && account add && oauth authorize && sync && list` works end-to-end against Nextcloud/Radicale/Apple (basic auth) and against Google Calendar (OAuth 2.0 device flow). Next up is **Milestone 8 — TUI**.

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

## Milestone 8 — TUI

- `tui/app.py` + `tui/bindings.py` + the screen / widget files in `ARCHITECTURE.md §1`.
- Day, week, month, agenda, todo-list views.
- Screen-owned bindings; footer shows current screen only (`CONVENTIONS.md §11`).
- Add `ai/TUI_TESTING_PLAN.md` **when the TUI lands, not before**; then implement `tests/test_tui_flows.py` per that plan.

**Acceptance:** all five views navigable; create / edit / trash flows work end-to-end against a seeded repo; TUI tests green.

## Milestone 9 — MCP server

- `mcp_server.py` — read-only tools: `list_calendars`, `query_range(start, end)`, `search(query)`, `get_event(uid)`, `get_todo(uid)`.
- MCP tests that stand up a server in-process and exercise each tool.

**Acceptance:** MCP server starts cleanly; each tool returns expected payloads against a seeded index; no write tools present.

## Milestone 10 — Packaging and release

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
