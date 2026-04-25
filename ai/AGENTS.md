# AGENTS.md

First doc every agent reads. Start here.

## 1. Project overview

`chronos` is a standalone Python 3.13 calendar client. One sentence: CalDAV sync → local iCalendar (`.ics`) mirror → SQLite index with recurrence-expansion cache → Textual TUI, with a CLI surface and a read-only MCP server on the side.

It is the calendar counterpart to the sibling `pony` mail user agent. The two projects share conventions, quality gates, and development discipline; the algorithms differ because CalDAV and IMAP differ.

## 2. Reading order

Read, in this order, before making any non-trivial change:

1. `ai/AGENTS.md` (this file)
2. `ai/SPECIFICATIONS.md` — what chronos is and is not
3. `ai/ARCHITECTURE.md` — package layout and subsystems
4. `ai/CONVENTIONS.md` — language, typing, testing, style, deps, release
5. `ai/SYNCHRONIZATION.md` — CalDAV sync algorithm and conflict taxonomy
6. `ai/RECURRENCE.md` — RRULE/RDATE/EXDATE expansion and the occurrence cache
7. `ai/TASKS.md` — current work queue

## 3. How to work

1. Read `ARCHITECTURE.md` before writing. Don't grep blindly to discover structure that is already documented.
2. Run all quality gates after every change:
   ```
   uv run ruff check src/ tests/
   uv run ruff format --check src/ tests/
   uv run mypy src/
   uv run basedpyright src/
   uv run python -m pytest --cov=chronos --cov-branch --cov-fail-under=85 tests/
   ```
   All must pass. No exceptions. Coverage is a gate, not a report.
3. **No speculative complexity.** No feature flags, no backwards-compatibility shims, no abstractions without a concrete caller. Three similar lines beat a premature abstraction.
4. Approved runtime dependencies only (see `CONVENTIONS.md §7`). New runtime deps require explicit approval before the `uv add` runs.
5. Keep `config-sample.toml`, `docs/`, and `ai/ARCHITECTURE.md` synchronised with code. If behaviour changes, one of these usually has to change too.
6. Do not touch the version string in `pyproject.toml` or `src/chronos/version.py`. The release workflow owns both and updates them atomically.
7. Tests use `unittest` classes, run via `pytest`. No mocking of SQLite or mirror storage — use real `SqliteIndexRepository` and real `.ics` files in a temp dir.

## 4. Local mutations and sync (load-bearing invariant)

A row in the `components` table with `href IS NULL` means: *"expected to exist on the server in this calendar, but not yet confirmed."*

That single signal covers every local-side pending change (new event, moved event, edited event). There is no separate "pending operations" table. The sync engine scans for `href IS NULL` rows and reconciles them against the server on the next pass.

This is the CalDAV analogue of pony's `uid IS NULL` convention. See `SYNCHRONIZATION.md §7` for how it drives the reconciliation steps, and §C-9 of the conflict taxonomy for local-move handling.

## 5. Coverage policy

We measure **branch coverage** with `pytest-cov` on the `chronos` package. Three rules govern every change:

1. **Floor is a ratchet.** `--cov-fail-under=85` is the CI floor. Never lower it. When project-wide branch coverage clears the next 5-point threshold (90, 95) by a comfortable margin in a green build, raise the floor in `pyproject.toml` to lock the gain in. The destination is 100%; the floor is the path.

2. **New code lands at 100%.** A new module, function, or branch you add must be fully covered by tests in the same change. "Hard to test" is a design signal — refactor the code (extract a pure function, inject a dependency, narrow the `try` block) until it's testable. Don't ship behaviour without a test.

3. **Exclusions are narrow, named, and justified.** A `# pragma: no cover` is allowed only with a trailing comment that names the reason. Acceptable reasons:
   - **platform fork** — the *other* platform's branch (`if sys.platform == "win32":` from a POSIX test run, or vice versa).
   - **`if TYPE_CHECKING:`** — never executed at runtime.
   - **unreachable defensive guard** — `raise AssertionError("unreachable: ...")` paths whose precondition is enforced upstream.
   - **`__main__` entry shim** — the `if __name__ == "__main__":` line in `__main__.py`.

   Anything else (HTTP error branches, "could happen if the server returns a malformed response", I/O fallbacks) is testable with a fake — write the fake. `pragma: no cover` without a comment, or for any other reason, is rejected.

`tests/` is excluded from measurement. Integration-only tests guarded by `CHRONOS_INTEGRATION=1` are excluded from the default run; coverage from them does not count toward the floor.

When a PR drops coverage below the floor, the fix is a test, never a `pragma`. When a PR drops it above the floor but below the previous run, investigate before merging — silent regressions are how floors slip.

## 6. Building the standalone executable

Chronos builds as a PyInstaller bundle the same way pony does. Commands will be:

```
uv sync --group build --group docs
uv run mkdocs build --strict
uv run python scripts/build.py
uv run python scripts/build.py --installer
```

`scripts/build.py`, `chronos.spec`, and `docs/` are Milestone-8 deliverables (see `TASKS.md`). This section is a placeholder until they land — do not add build infrastructure ahead of its milestone.

## 7. What NOT to do

1. Don't mock the SQLite database in tests. Use real `SqliteIndexRepository` with a temp-dir path.
2. Don't add `# type: ignore` without a specific diagnostic code (e.g. `# type: ignore[arg-type]`). Bare `ignore` is rejected.
3. Don't reach into private attributes from outside a class. Screens use public Textual API (`self.app.push_screen`, `self.app.notify`) — never `self.app._something`.
4. Don't create a separate "pending mutations" / "outbox" table. The `href IS NULL` signal on `components` is the only local-mutation mechanism (see §4).
5. Don't commit the `site/` MkDocs build artifact. It is generated at build time and gitignored.
6. Don't add write or mutating tools to the MCP server without explicit approval. The MCP surface is read-only by design.
7. Don't create documentation files unless asked. Edit existing ones.
8. No emojis in code, commits, or docs.
9. Don't expand recurrences on write. RRULE expansion is a read-time concern served by the `occurrences` cache. See `RECURRENCE.md`.
10. Don't invent synthetic UIDs on the write path. Synthetic UIDs exist only to defensively ingest malformed server data (see `SYNCHRONIZATION.md §C-8`).
11. Don't lower the `--cov-fail-under` floor or sprinkle `# pragma: no cover` to make red builds green. The fix is a test (see §5).
