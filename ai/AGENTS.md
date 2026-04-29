# AGENTS.md

Start here before making non-trivial changes.

## Reading order

1. `ai/AGENTS.md`
2. `ai/PLAN.md`
3. `ai/SPECIFICATIONS.md`
4. `ai/ARCHITECTURE.md`
5. `ai/CONVENTIONS.md`
6. `ai/SYNCHRONIZATION.md`
7. `ai/RECURRENCE.md`

## Project summary

`chronos` is a Python 3.13 calendar client:

- CalDAV sync
- local `.ics` mirror
- SQLite search/index/occurrence cache
- Textual TUI
- CLI
- MCP tools, including additive `.ics` import

## Working rules

1. Read the architecture and conventions docs before editing code.
2. Keep docs synchronized with code, especially `PLAN.md`, `SPECIFICATIONS.md`, `ARCHITECTURE.md`, `CONVENTIONS.md`, `config-sample.toml`, and `README.md`.
3. Sacrifice grammar for conciseness, token economy and clarity.
4. Run the quality gates after changes:
   ```
   uv run ruff check src/ tests/
   uv run ruff format --check src/ tests/
   uv run mypy src/
   uv run basedpyright src/
   uv run python -m pytest --cov=chronos --cov-branch --cov-fail-under=85 tests/
   ```
5. Do not add runtime dependencies without explicit approval.
6. Do not edit release versions manually during routine development. Tagged release automation owns `pyproject.toml`, `src/chronos/version.py`, and the stamped changelog heading.
7. Use real SQLite and real mirror files in tests; do not mock the persistence layer.

## Load-bearing invariant

`href IS NULL` in the local component row means the component should exist on the server but has not yet been confirmed there. That signal drives local pending pushes and imported data.

## Important constraints

- No destructive MCP tools.
- No browser-based application UI work.
- No speculative abstractions or compatibility shims without a real caller.
- Coverage floor is a gate, not a suggestion.
