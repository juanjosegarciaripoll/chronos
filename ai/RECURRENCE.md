# RECURRENCE.md

RRULE / RDATE / EXDATE expansion and the `occurrences` cache. Load-bearing for any code that touches recurring components.

## 1. Principles

1. **Expansion is a read-time concern.** We never materialise occurrences eagerly on write. Writes update the master (and overrides) and invalidate the cache.
2. **The master stores recurrence rules verbatim.** The `components` row for a master VEVENT / VTODO keeps the original `RRULE`, `RDATE`, and `EXDATE` properties untouched in its `raw_ics` and in projected columns.
3. **Overrides are separate rows.** An override is a `components` row sharing the master's `UID` with a non-null `recurrence_id`. It carries the full override VEVENT / VTODO, not a patch.
4. **The `occurrences` table is a rebuildable cache.** It holds pre-expanded rows for a bounded window. It can be dropped and rebuilt at any time without data loss.

## 2. Expansion contract

```
recurrence.expand(master: VEvent | VTodo,
                  overrides: Sequence[VEvent | VTodo],
                  window_start: datetime,
                  window_end: datetime) -> list[Occurrence]
```

Returns an ordered list of `Occurrence(component_id, start, end, recurrence_id, is_override)` covering `[window_start, window_end)`. The function is pure: same inputs → same output.

For non-recurring components, `expand` returns a single `Occurrence` if the component's DTSTART falls in the window, else an empty list.

## 3. Library choice

- `python-dateutil`'s `rrule` / `rruleset` handles FREQ, INTERVAL, BYxxx, COUNT, UNTIL, WKST.
- `EXDATE` values feed `rruleset.exdate(...)`.
- `RDATE` values feed `rruleset.rdate(...)`.
- Overrides are applied after `rruleset` expansion: for each expanded instant, if an override exists whose `RECURRENCE-ID` matches (compared as timezone-aware datetimes), replace the expanded occurrence with the override's DTSTART/DTEND.
- The `icalendar` library parses the raw properties; we do not hand-roll RRULE parsing.

## 4. Cache policy

- `occurrences` rows are populated lazily on view. Each view computes its needed window and asks the index for expanded occurrences; missing ones are expanded and inserted.
- Default cached window: **current month ± 12 months**. Configurable per deployment later; out of scope for v1.
- Invalidation:
  - Any write to a master row → delete all `occurrences` rows for that `component_id`.
  - Any write to an override row → delete the single `occurrences` row at its `recurrence_id`.
  - VTIMEZONE change on the server (§C-11) → delete all `occurrences` rows for components referencing that TZID.
- Rebuild is lazy. We never rebuild the whole cache on startup.

## 5. Edge cases

- **All-day vs timed.** DTSTART as DATE (no TZ) vs DATE-TIME (with TZ). Store both as timestamps; all-day uses 00:00 local at the floating zone defined by the component.
- **DST transitions.** Floating events (no TZID) shift with local DST; TZ-anchored events keep their wall-clock time. `python-dateutil` handles both if the TZID is registered; we register VTIMEZONEs from the source iCalendar before expanding.
- **UNTIL in a different TZ from DTSTART.** RFC 5545 requires UNTIL in UTC when DTSTART is timed. When a server supplies UNTIL in the DTSTART TZ (a common bug), we normalise to UTC at expansion time and log.
- **Zero-duration events.** DTEND == DTSTART, or DTEND missing. Treated as a point event; the `occurrences` row has `occurrence_end IS NULL`.
- **Infinite RRULE** (no COUNT, no UNTIL). Capped at `window_end` during expansion. We never materialise beyond the window.
- **Count vs until.** Both constrain the series; `rruleset` handles the precedence correctly.
- **RECURRENCE-ID with RANGE=THISANDFUTURE.** v1 treats the override as a single-instance override only. Full THISANDFUTURE semantics are deferred.

## 6. What NOT to do

- **Don't expand on write.** Writes must complete without consulting `python-dateutil`. Expansion happens when a view reads.
- **Don't materialise beyond the cache window.** Even if a user scrolls past the edge, the view triggers an extension, not an up-front expansion to infinity.
- **Don't modify overrides by mutating the master.** Editing a single occurrence means creating (or updating) the RECURRENCE-ID override, not rewriting the RRULE.
- **Don't store `EXDATE` as a denormalised list on the master row.** Keep it in `raw_ics`; project it at expansion time.
- **Don't trust VTIMEZONE definitions blindly across servers.** Register per-component as part of expansion; don't rely on a process-global tzdata cache.
