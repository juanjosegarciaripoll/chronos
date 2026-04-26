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

- `src/chronos/oauth.py`: loopback flow (RFC 8252 + PKCE), token store (`save_tokens` / `load_tokens` under `paths.oauth_token_dir()`), bearer-token HTTP auth (`BearerTokenAuth` subclass of `niquests.auth.AuthBase`) with automatic refresh-grant on expiry.
- `src/chronos/authorization.py`: `Authorization` carrying either basic (username, password) or `http_auth` (AuthBase) plus an `on_commit` callback for token rotation.
- `src/chronos/domain.py`: `OAuthCredential` (client_id, client_secret, scope, optional token_path) added to the `CredentialSpec` union.
- `src/chronos/credentials.py`: `build_auth(account)` returns `Authorization`; OAuth accounts wire through `oauth.build_bearer_auth`.
- `src/chronos/caldav_client.py`: `CalDAVHttpSession` accepts `Authorization`; basic goes via `DAVClient(username, password)`, bearer goes via `DAVClient(auth=...)` (niquests AuthBase).
- `src/chronos/cli.py`: `account add --credential-backend oauth --oauth-client-id ... --oauth-client-secret ... --oauth-scope ...`; new `chronos oauth authorize --account NAME` re-runs the loopback flow.

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

## Milestone 12 — Incremental sync via WebDAV sync-collection

### The problem

The sync engine has two paths today:

- **Fast path** (CTag unchanged): zero extra I/O. Perfect.
- **Slow path** (CTag changed): issues a `calendar-query` REPORT that returns `(href, etag)` for **every** resource in the calendar. For Google Holiday calendars (10 000+ events) this is several megabytes of XML per sync, even when only one event changed. The slow path then computes the diff and only multigets the actual delta, so it is already better than fetching all bodies, but the index scan alone is expensive.

`SyncState.sync_token` and the `sync_token` column in `calendar_sync_state` exist in the schema but are always written as `NULL` (`sync.py` line 265). The `SYNCHRONIZATION.md §6` describes the medium path but it has never been implemented.

### What WebDAV sync-collection (RFC 6578) provides

`sync-collection` is a REPORT that answers: "give me only what changed since sync-token T, and here is your new token." Google Calendar, Fastmail, iCloud, and Nextcloud all support it. The server returns:

- 200-propstat entries: hrefs that were added or modified (with their new etags).
- 404-propstat entries: hrefs that were deleted.
- A new `<d:sync-token>` at the end of the multistatus body.

For a typical "one event edited" sync this reduces the round-trip to a single small REPORT response + one multiget, instead of a full calendar-query scan.

### Changes required

**A. `protocols.py` — two new methods on `CalDAVSession`**

```python
def sync_collection(
    self, calendar_url: str, sync_token: str
) -> tuple[
    Sequence[tuple[str, str]],  # (href, etag) — added or changed
    Sequence[str],              # hrefs deleted on the server
    str,                        # new sync-token to store
]: ...

def get_sync_token(self, calendar_url: str) -> str | None: ...
```

Both are optional in the sense that the engine falls back to the slow path when the server doesn't support them, but every `CalDAVSession` implementation (real and fake) must declare them.

**B. `caldav_client.py` — implement both new methods**

`sync_collection`:
- Issues a raw `sync-collection` REPORT with the body below, depth 1.
- Parses the multistatus: 200-propstat entries → changed, 404-propstat entries → deleted.
- Extracts `<d:sync-token>` from the multistatus root (not inside a `<d:response>`).
- On 403 or 409 where the body contains `<d:valid-sync-token/>`: raises `SyncTokenExpiredError` (new subclass of `CalDAVError`). The engine catches this and falls back to the slow path.
- On any other 4xx/5xx: raises the existing `CalDAVError`.

Request body:
```xml
<?xml version="1.0" encoding="utf-8"?>
<d:sync-collection xmlns:d="DAV:">
  <d:sync-token>TOKEN</d:sync-token>
  <d:sync-level>1</d:sync-level>
  <d:prop><d:getetag/></d:prop>
</d:sync-collection>
```

`get_sync_token`:
- Issues a `PROPFIND` at depth 0 asking for `DAV:sync-token`.
- Returns the token string, or `None` if the server doesn't include it (servers without sync-collection support will return an empty propstat).

Request body:
```xml
<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:">
  <d:prop><d:sync-token/></d:prop>
</d:propfind>
```

**C. `sync.py` — medium path and token acquisition**

1. Add `SyncTokenExpiredError` import.
2. Extend `CalendarSyncStats.path` to `Literal["fast", "medium", "slow"]`.
3. Add `_medium_path_reconcile()` mirroring `_slow_path_reconcile()`:
   - Calls `session.sync_collection(url, stored_token)` → `(changed, deleted, new_token)`.
   - Runs `_guard_mass_deletion` on `deleted` (same threshold as slow path).
   - Calls `_apply_server_deletions` for the deleted hrefs (reuse unchanged).
   - Calls `_fetch_and_ingest` for the changed hrefs (reuse unchanged; the pipelined producer/consumer carries over verbatim).
   - Calls `_push_trashed` and `_push_pending` for local mutations (same as slow path).
   - Returns `CalendarSyncStats(path="medium", ...)` and the `new_token`.
4. Update `_sync_calendar()` path selection:

```
CTag matches
  → fast path (unchanged)

CTag changed + stored sync_token is not None
  → try medium path
      success  → store new_token in SyncState
      SyncTokenExpiredError
               → clear stored token, run slow path,
                 then call get_sync_token() to acquire a fresh token

CTag changed + no stored sync_token
  → slow path, then call get_sync_token() to acquire token for next time
```

5. In `_sync_calendar`, update the `set_sync_state` call so `sync_token` is populated from the result of `get_sync_token` / the medium path's `new_token` rather than always `None`.

**D. `tests/fake_caldav.py` — extend `FakeCalDAVSession`**

Add internal state:
- `_sync_tokens: dict[str, str]` — current sync-token per calendar URL.
- `_change_log: dict[str, list[tuple[str, Literal["added", "changed", "deleted"]]]]` — ordered log of changes since a given token, keyed by calendar URL. Or simpler: a monotone counter per calendar and a per-token snapshot of the state.

Implement `sync_collection(url, token)`:
- If `token` is unknown or expired (special "expired" sentinel): raise `FakeCalDAVSyncTokenExpiredError`.
- Otherwise compute `(changed, deleted)` as the diff between the snapshot at `token` and the current state, return those plus the new current token.

Implement `get_sync_token(url)`:
- Return current sync token for the calendar.

Existing `put_resource` / `remove_resource` helpers advance the sync token alongside bumping the CTag.

**E. `tests/test_sync.py` — new test class `MediumPathTest`**

Cases to cover (all 100% branch-covered per AGENTS §5):

1. **Happy path**: CTag changed + valid sync_token → medium path; exactly the changed hrefs are multiget-fetched (no `calendar_query` call); deleted hrefs removed; new token stored.
2. **Expired token falls back**: `SyncTokenExpiredError` → slow path runs; new token acquired via `get_sync_token`; stored in index; next sync uses medium path.
3. **No prior token**: first-ever sync with CTag mismatch → slow path; `get_sync_token` called once; token stored for next run.
4. **Fast path unaffected**: CTag unchanged → neither `sync_collection` nor `calendar_query` called.
5. **Mass-deletion guard on medium path**: >20% of known hrefs deleted on server → `SyncHaltError` raised from medium path (same guard as slow path).

Update existing slow-path tests to assert `get_sync_token` is called once at the end and that `index.get_sync_state` then returns a non-None `sync_token`.

**F. `SYNCHRONIZATION.md` — update §6**

Replace the "Medium path — … (described but not yet implemented)" note with the actual logic. Update §9 (performance properties) to note that the medium path's I/O is bounded by the size of the change set, not the total calendar size.

### What does not change

- Fast path is completely untouched.
- Slow path is kept as-is (fallback for expired tokens and servers without sync-collection).
- `_fetch_and_ingest` pipelined producer/consumer is reused by the medium path unchanged.
- `_guard_mass_deletion` applies equally to medium-path deletions.
- The conflict taxonomy (C-1 through C-11) is unchanged; C-4 is the one that fires on token expiry.
- No schema changes: `sync_token` column already exists in `calendar_sync_state`.

### Known limitations to document (not block the milestone)

- Google's sync-token expires after roughly 30 days of inactivity. The expired-token fallback handles this gracefully.
- RFC 6578 allows a server to return a truncated response (too many changes). In practice Google doesn't, but the engine should treat a `507 Insufficient Storage` response from `sync_collection` the same as a `SyncTokenExpiredError`: clear the token, fall back to slow path.
- CTag drift on Google (a new CTag returned on every PROPFIND) is already handled by the re-read-after-push logic in `_sync_calendar`; no interaction with the medium path.

**Acceptance:** medium path exercises zero `calendar_query` calls for a single-event change on a calendar with a stored token; expired-token test passes without manual intervention; all conflict cases still covered; project-wide branch coverage stays ≥ 85%.

## Milestone 13 — ICS file ingestion

Let users (and AI agents) pull an external `.ics` file — meeting invites, exports from another client, a colleague's published feed — into a local calendar, where it then flows through the normal `href IS NULL` push path on the next sync. This is also the first additive-write surface on the MCP server. The MCP rule we actually care about is "no destructive tools" (no event deletion, no calendar deletion / rename, no bulk overwrite of existing data) — additive ingestion is in scope. Update `ai/AGENTS.md §7.6` and `ai/SPECIFICATIONS.md §3` accordingly when this milestone ships.

**A. Shared ingestion core — `src/chronos/ingest.py` (new module)**

One function does the actual work; CLI and MCP wrap it.

```
def ingest_ics_bytes(
    payload: bytes,
    *,
    target: CalendarRef,
    mirror: MirrorRepository,
    index: IndexRepository,
    on_conflict: Literal["skip", "replace", "rename"] = "skip",
) -> IngestReport: ...
```

- Parses via `ical_parser` into a list of `VEvent` / `VTodo` (rejects `VJOURNAL` / `VFREEBUSY` with a clear error — out of v1 scope per `SPECIFICATIONS.md §4`).
- For each component:
  - If `UID` is missing or empty, generate one via `mutations.generate_uid` (this is the one place we synthesise on the *write* path; defensive, not for round-trip — note in `ai/AGENTS.md §7.10` is about server data, this is user-supplied input, so the rule does not apply, but document the deviation in the module docstring).
  - Look up `(target, uid)` in the index. On collision: `skip` (default), `replace` (overwrite mirror + index, keep existing href if any), or `rename` (assign a fresh UID and ingest as new).
  - Write the `.ics` to the mirror via `MirrorRepository.write_new` / `overwrite`, then project into the index via `storage_indexing` with `href = NULL` so the next `chronos sync` pushes it.
- `IngestReport` captures `imported`, `skipped`, `replaced`, `renamed`, plus per-component reasons. Returned to caller for display / tool response.
- Recurrence overrides (multiple `VEVENT`s sharing a UID with `RECURRENCE-ID`) ingest as a single mirror file — same shape as a sync-fetched master + override bundle (`RECURRENCE.md §3`).

**B. CLI — `chronos import` subcommand (in `cli.py`)**

- `chronos import PATH [PATH ...] [--account NAME] [--calendar NAME] [--on-conflict {skip,replace,rename}]`
- `PATH` may be a file or a directory; directories are walked for `*.ics` non-recursively (recursive ingest is a future ask, not now).
- Calendar resolution:
  1. Both flags supplied → resolve directly; error if account or calendar unknown.
  2. Only `--account` supplied → list that account's calendars and prompt for one (use the existing `default_prompt` infra; non-interactive stdin → exit 2 with a clear message naming `--calendar`).
  3. Neither supplied → list every `(account, calendar)` known to the index, prompt the user with a numbered menu.
- Re-uses the bootstrap-style prompt (`bootstrap.PromptFn` / `default_prompt`) so the existing test harness for `chronos init` covers the prompt path.
- Prints the `IngestReport` summary on stdout; exits non-zero if every component was skipped due to conflicts and `--on-conflict skip` was in effect (so scripts notice).

**C. MCP — `import_ics` tool (in `mcp_server.py`)**

Registered alongside the existing five read tools, no opt-in flag. Additive writes are allowed; the MCP server still has no event-delete or calendar-delete tools, and `on_conflict="replace"` is the only path that overwrites an existing UID — by explicit caller request, on a single component, never in bulk.

Tool schema:
```
import_ics(
    account: str,           # required
    calendar: str,          # required
    ics: str,               # raw RFC 5545 text
    on_conflict: "skip" | "replace" | "rename" = "skip"
) -> { imported: int, skipped: int, replaced: int, renamed: int, details: [...] }
```

- Both `account` and `calendar` are **required** inputs (no interactive prompting — MCP has no user-facing UI). Missing or unknown account/calendar → tool error with the list of valid `(account, calendar)` pairs in the error body so the agent can retry.
- The tool calls the same `ingest.ingest_ics_bytes` core. The MCP layer does no policy of its own beyond input validation.
- Update `mcp_server.py` module docstring + `ai/SPECIFICATIONS.md §3` and `ai/AGENTS.md §7.6` to record the actual rule: "MCP tools may add data but not destroy it — no delete-event, no delete-calendar, no bulk-overwrite tools."

**D. Tests**

- `tests/test_ingest.py` — unit tests for `ingest_ics_bytes` against a real `SqliteIndexRepository` + temp-dir mirror: single VEVENT, recurring master + override bundle, missing-UID synthesis, all three `on_conflict` modes, VJOURNAL rejection, malformed ICS rejection.
- `tests/test_cli.py` — extend with `import` cases: file argument with both flags, directory argument, missing-flag interactive prompt (driven through the captured-stdout harness with a scripted `PromptFn`), non-interactive missing-flag → exit 2.
- `tests/test_mcp_server.py` — extend with `import_ics`: happy path, missing account / calendar errors include the valid-pairs list, all three conflict modes round-trip, and a regression test that no destructive tool (`delete_event`, `delete_calendar`, etc.) is registered on the server.
- Followup sync test (in `tests/test_sync.py` or a new `test_ingest_sync.py`): ingest a file, then run a sync against `FakeCalDAVSession` and assert the imported component PUTs to the server (proving the `href IS NULL` plumbing carries through).

**E. Docs**

- `ai/ARCHITECTURE.md §1` — add `ingest.py` to the package layout listing.
- `ai/ARCHITECTURE.md §2` — one-paragraph "Ingestion" subsystem entry pointing at the shared core and noting the two entry points (CLI, opt-in MCP).
- `config-sample.toml` and `docs/` — no schema change needed, but document the new commands once `docs/` lands (M11).

**Acceptance:** `chronos import some.ics` against a single-account, single-calendar config drops the user into a one-line menu and ingests; `chronos import --account a --calendar c some.ics` runs non-interactively; `chronos sync` afterwards pushes the imported component to the server; the MCP `import_ics` tool is registered, refuses missing/unknown calendar inputs with a useful error, and the server still exposes no destructive tool; ingestion tests cover all three conflict modes; project-wide branch coverage stays ≥ 85%.

## Milestone 14 — MCP TCP transport and stdio bridge

Replace the single-transport `chronos mcp` command with a three-mode architecture that lets the TUI and future daemon modes share their running MCP session with external clients (Claude Desktop, editor integrations) without opening the calendar mirror from two processes. See `ai/MCP.md` for the detailed design; this milestone implements it. The `--port` and `--host` flags introduced in M10 are removed; transport selection is now automatic.

**A. `src/chronos/paths.py`** — add `mcp_server_state_path() -> Path` returning `user_data_dir() / "mcp_server.json"`. No new dependency: the existing per-platform `user_data_dir()` is sufficient.

**B. `src/chronos/mcp_state.py`** (new module)

```
@dataclass(frozen=True)
class McpServerState:
    port: int
    token: str

def write_state(state: McpServerState) -> None   # atomic temp+replace; chmod 0600 on POSIX
def read_state() -> McpServerState | None        # None if file missing or malformed
def remove_state() -> None                       # silent if already absent
```

State file is JSON: `{"port": N, "token": "..."}`. Uses `paths.mcp_server_state_path()`.

**C. `src/chronos/mcp_transport.py`** (new module)

Three async functions, all running under anyio (asyncio backend):

`serve_tcp(server, *, state)` — accepts connections on `127.0.0.1:state.port` via `asyncio.start_server`. Per-connection flow:
1. Read first line within `AUTH_TIMEOUT = 2.0 s`; parse as `{"auth": "<token>"}`.
2. If token mismatch or timeout: close connection silently.
3. Create two anyio memory object streams: `in_send/in_recv` typed `JSONRPCMessage | Exception` and `out_send/out_recv` typed `JSONRPCMessage`.
4. Run three concurrent tasks inside `anyio.create_task_group()`:
   - TCP reader: read lines → `JSONRPCMessage.model_validate_json(line)` → send to `in_send`.
   - TCP writer: receive from `out_recv` → `msg.model_dump_json(by_alias=True, exclude_none=True)` + `\n` → write to `asyncio.StreamWriter`.
   - MCP session: `await server.run(in_recv, out_send, server.create_initialization_options())`.
5. Any pump task finishing (client disconnect, parse error) cancels the others.

`run_stdio_bridge(state)` — transparent stdio↔TCP forwarder:
1. Open TCP connection to `127.0.0.1:state.port` with `CONNECT_TIMEOUT = 0.5 s`.
2. Send auth frame: `{"auth": state.token}\n`.
3. Two tasks: `stdin.buffer → TCP writer` and `TCP reader → stdout.buffer`. Reads on stdin use `asyncio.get_event_loop().run_in_executor(None, ...)` to avoid blocking the event loop.

`run_stdio_standalone(server)` — self-contained mode for when no TCP server is running. Uses `mcp.server.stdio.stdio_server()` exactly as the old `serve_stdio` did.

**D. `src/chronos/mcp_server.py`** — replace `run_mcp_server(host, port)` with:

`run_mcp_stdio(*, index, mirror)` — the single entry point for `chronos mcp`:
1. Call `read_state()`.
2. If a state exists: attempt `asyncio.open_connection("127.0.0.1", state.port)` within `CONNECT_TIMEOUT`. On success, call `run_stdio_bridge(state)` and return.
3. On `ConnectionRefusedError`, `TimeoutError`, or any `OSError`: call `remove_state()`, fall through to step 4.
4. Self-contained: `server = build_server(index=index, mirror=mirror)`, `await run_stdio_standalone(server)`.

`start_tcp_server(*, index, mirror, port=0)` — starts the TCP server and writes the state file. Returns `McpServerState`. Intended for the TUI and future daemon; not called from the CLI. Port 0 means OS assigns an ephemeral port; the actual port is read back from the server socket and written to the state file. Removes the state file on exit (run inside `try/finally`).

**E. `src/chronos/cli.py`**

- Remove `--host` and `--port` from the `mcp` subparser.
- `cmd_mcp(ctx)` uses `anyio.run` to call `run_mcp_stdio(index=ctx.index, mirror=ctx.mirror)` (no arguments beyond the repositories).

**F. `tests/test_mcp_transport.py`** (new)

`FramingTest` — start a real TCP server on an ephemeral port, connect, send auth, exchange a valid MCP `initialize` → `initialized` handshake, assert the response arrives.

`AuthTest` — wrong token and missing auth frame both result in the connection being closed before any MCP data flows.

`BridgeDetectionTest` — `run_mcp_stdio` with a stale state file (port where nothing listens) calls `remove_state()` and goes self-contained; state file is gone after the run.

`BridgeForwardingTest` — start a TCP server in one task; start a bridge in another; exchange at least one MCP message end-to-end through the bridge.

**G. Docs**

`ai/ARCHITECTURE.md §1` — add `mcp_state.py` and `mcp_transport.py`.
`ai/ARCHITECTURE.md §2` — update the MCP subsystem description.
`ai/CONVENTIONS.md §7` — note that `anyio` is available as a transitive dep via `mcp` and may be used in transport code; no explicit `uv add` needed.

**Acceptance:** `chronos mcp` with no state file goes self-contained and responds to `list_tools`; with a stale state file it cleans up and goes self-contained; `start_tcp_server` writes the state file and a `chronos mcp` run launched in the same test bridges to it; auth rejection test passes; project-wide branch coverage stays ≥ 85%.

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
- **OAuth refresh-failure auto re-auth** — token *expiry while authorized* refreshes automatically inside `BearerTokenAuth.__call__`. If the refresh grant itself fails (revoked refresh token, scope changed, etc.), the error currently bubbles up through niquests as a request failure. The provider should detect the refresh-failure case and re-run the loopback flow, mirroring the missing-tokens path. Needs a way to surface the error from inside the auth callable back to the credentials provider.
- **Hybrid occurrence expansion** — `sync_account` currently calls `populate_occurrences` with a wide static window (`now - 30y` to `now + 5y`) per calendar. For typical calendars (≤100 masters, mostly weekly/monthly recurrences) this writes a few thousand rows total — fine. For a daily-forever event over 35 years it hits `MAX_OCCURRENCES=10_000` and the master is silently skipped (no rows for it; invisible in the TUI). The right long-term fix is per-master smart windowing: non-recurring masters get a 1-row cache regardless of window (window-independent), recurring masters cache a narrow window like `[today - 1y, today + 2y]`, and `views.gather_occurrences` falls back to on-demand `expand()` when a query window extends beyond the cached range. This keeps the cache small while preserving SQL-fast common-case queries. Out of scope for v1; revisit when a heavy calendar surfaces problems.
- **TUI sync on a Textual worker** — `MainScreen._run_sync` currently calls the sync_runner synchronously on the UI thread, so the TUI is frozen for the duration and a Ctrl-C exits the app rather than just cancelling the sync. The persistence layers all release cleanly under interrupt (lockfile contextmanager, SQLite per-resource transactions, atomic mirror writes), so the on-disk state is consistent — but the UX would be better if sync ran on a Textual worker (`@work`) with a status banner and a "cancel sync" key. Out of scope for the crash-safety milestone (correctness done); revisit when polishing the TUI.
- **Pipelined fetch + ingest** — _Shipped._ `sync._fetch_and_ingest` runs the network fetch on a daemon producer thread and the parse + mirror-write + index-upsert on the main consumer thread, drained through a bounded queue (`_FETCH_PIPELINE_BUFFER = 2` chunks of `_FETCH_CHUNK_SIZE = 100` hrefs each). SQLite stays single-writer because only the consumer touches the index; the producer talks to one `CalDAVHttpSession` from one thread, so niquests' non-thread-safety is moot. Producer-side exceptions funnel through the queue and re-raise on the consumer; the consumer's `finally` flips a cancel event and `join`s the producer so a crashed sync can't leak threads. Verified by `tests/test_sync.py::FetchIngestPipelineTest` (deterministic overlap test deadlocks if the second multiget waits on the first ingest to finish).
