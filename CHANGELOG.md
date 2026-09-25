# Changelog

All notable changes to chronos are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Multi-account CalDAV sync with fast, medium, and slow reconciliation paths.
- Textual TUI with agenda, day, and grid views, plus create, edit, trash, search, and sync flows.
- MCP server with stdio mode, TCP bridge mode, query tools, and iTIP-aware `.ics` import.
- iTIP-aware `.ics` import: `METHOD:CANCEL` trashes the matching event and `METHOD:REQUEST`/newer `SEQUENCE` updates it in place. Changes to already-synced events propagate to the server on the next sync (DELETE for cancellations, If-Match PUT for updates).
- The TUI highlights the current moment: in Day / Grid views the current half-hour slot gets a `▸` time label (and a "now" line when empty) and the event in progress is filled with the theme accent; in the Agenda view events in progress are bold, accent-coloured, and marked `▸`. The highlight follows the clock without moving the cursor.
- Alarm notifications say when the event starts (dropping Google's "This is an event reminder" boilerplate), no longer fire twice when a sync rebuilds the alarm cache, and log failures to `tui.log`.
- The TUI syncs in the background while it is open, hourly by default (`background_sync_enabled`, `background_sync_interval_seconds`). A countdown next to the view title shows when the next run is due. `g` now syncs immediately without a dialog and restarts the countdown; the confirm + progress dialog moved to `G`.
- Edits to already-synced events made in the TUI or with `chronos edit` are pushed to the server on the next sync.
- `chronos import` syncs the target account after importing so changes reach the server right away. Interactive terminals are asked to confirm (`-y` skips the question); `--no-sync` leaves the changes for the next `chronos sync`.
- OAuth loopback authorization flow for providers that require browser-based consent.
- Tagged release automation for source builds, Windows installers, and Windows portable PyInstaller bundles.
