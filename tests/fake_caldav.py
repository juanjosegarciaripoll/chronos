from __future__ import annotations

from collections.abc import Sequence

from chronos.caldav_client import SyncTokenExpiredError
from chronos.domain import ComponentKind, RemoteCalendar


class FakeCalDAVError(Exception):
    pass


class FakeCalDAVConflictError(FakeCalDAVError):
    pass


class FakeCalDAVNotFoundError(FakeCalDAVError):
    pass


class FakeCalDAVSession:
    """Deterministic in-memory CalDAVSession for testing.

    Expose a `calls` log of every Protocol method invocation so sync
    tests can assert on I/O minimality (fast-path zero-query etc.).

    Sync-token tracking: each `put_resource` / `remove_resource` bumps
    a per-calendar monotone counter and appends to a change log. The
    token format is `"tok-{counter}"`. `sync_collection` replays the
    log entries since the given token; `expire_sync_token` poisons a
    token so it raises `SyncTokenExpiredError` on the next use.
    """

    def __init__(self, *, principal_url: str = "/dav/principals/user/") -> None:
        self._principal_url = principal_url
        self._calendars: dict[str, RemoteCalendar] = {}
        self._resources: dict[str, dict[str, bytes]] = {}
        self._etags: dict[str, str] = {}
        self._ctags: dict[str, str] = {}
        # sync-token state
        self._tok_counters: dict[str, int] = {}
        self._tok_log: dict[str, list[tuple[int, str, str]]] = {}
        self._tok_expired: set[str] = set()
        self.calls: list[tuple[object, ...]] = []

    # --- test helpers --------------------------------------------------------

    def add_calendar(
        self,
        *,
        url: str,
        name: str,
        ctag: str = "ctag-0",
        supported_components: frozenset[ComponentKind] = frozenset(
            {ComponentKind.VEVENT, ComponentKind.VTODO}
        ),
    ) -> None:
        self._calendars[url] = RemoteCalendar(
            name=name, url=url, supported_components=supported_components
        )
        self._resources.setdefault(url, {})
        self._ctags[url] = ctag
        self._tok_counters[url] = 0
        self._tok_log[url] = []

    def put_resource(
        self, *, calendar_url: str, href: str, ics: bytes, etag: str
    ) -> None:
        action = "changed" if href in self._resources.get(calendar_url, {}) else "added"
        self._resources[calendar_url][href] = ics
        self._etags[href] = etag
        self._bump_ctag(calendar_url)
        self._bump_tok(calendar_url, href, action)

    def remove_resource(self, calendar_url: str, href: str) -> None:
        self._resources[calendar_url].pop(href, None)
        self._etags.pop(href, None)
        self._bump_ctag(calendar_url)
        self._bump_tok(calendar_url, href, "deleted")

    def set_ctag(self, calendar_url: str, ctag: str) -> None:
        self._ctags[calendar_url] = ctag

    def expire_sync_token(self, calendar_url: str) -> None:
        """Poison the current sync-token so the next sync_collection call fails."""
        tok = f"tok-{self._tok_counters.get(calendar_url, 0)}"
        self._tok_expired.add(tok)

    def current_ctag(self, calendar_url: str) -> str | None:
        return self._ctags.get(calendar_url)

    def current_sync_token(self, calendar_url: str) -> str:
        return f"tok-{self._tok_counters.get(calendar_url, 0)}"

    def hrefs_in(self, calendar_url: str) -> tuple[str, ...]:
        return tuple(self._resources.get(calendar_url, {}).keys())

    def etag_for(self, href: str) -> str | None:
        return self._etags.get(href)

    # --- internal ------------------------------------------------------------

    def _bump_ctag(self, calendar_url: str) -> None:
        prev = self._ctags.get(calendar_url, "ctag-0")
        head, _, tail = prev.rpartition("-")
        try:
            self._ctags[calendar_url] = f"{head or 'ctag'}-{int(tail) + 1}"
        except ValueError:
            self._ctags[calendar_url] = f"{prev}+1"

    def _bump_tok(self, calendar_url: str, href: str, action: str) -> None:
        counter = self._tok_counters.get(calendar_url, 0) + 1
        self._tok_counters[calendar_url] = counter
        self._tok_log.setdefault(calendar_url, []).append((counter, href, action))

    def _owning_calendar(self, href: str) -> str | None:
        for cal_url, resources in self._resources.items():
            if href in resources:
                return cal_url
        return None

    # --- CalDAVSession protocol ----------------------------------------------

    def discover_principal(self) -> str:
        self.calls.append(("discover_principal",))
        return self._principal_url

    def list_calendars(self, principal_url: str) -> Sequence[RemoteCalendar]:
        self.calls.append(("list_calendars", principal_url))
        return tuple(
            RemoteCalendar(
                name=cal.name,
                url=url,
                supported_components=cal.supported_components,
                ctag=self._ctags.get(url),
                sync_token=(
                    self.current_sync_token(url) if url in self._tok_counters else None
                ),
            )
            for url, cal in self._calendars.items()
        )

    def get_ctag(self, calendar_url: str) -> str | None:
        self.calls.append(("get_ctag", calendar_url))
        return self._ctags.get(calendar_url)

    def calendar_query(self, calendar_url: str) -> Sequence[tuple[str, str]]:
        self.calls.append(("calendar_query", calendar_url))
        resources = self._resources.get(calendar_url, {})
        return tuple((href, self._etags[href]) for href in resources)

    def calendar_multiget(
        self, calendar_url: str, hrefs: Sequence[str]
    ) -> Sequence[tuple[str, str, bytes]]:
        self.calls.append(("calendar_multiget", calendar_url, tuple(hrefs)))
        resources = self._resources.get(calendar_url, {})
        return tuple(
            (href, self._etags[href], resources[href])
            for href in hrefs
            if href in resources
        )

    def put(self, href: str, ics: bytes, etag: str | None) -> str:
        self.calls.append(("put", href, etag))
        owner = self._owning_calendar(href)
        if etag is None:
            # If-None-Match: * — must not exist.
            if owner is not None:
                raise FakeCalDAVConflictError(f"PUT If-None-Match failed for {href}")
            target = self._calendar_for_new_href(href)
            if target is None:
                raise FakeCalDAVNotFoundError(f"no calendar matches href {href}")
            new_etag = self._next_etag(href)
            self._resources[target][href] = ics
            self._etags[href] = new_etag
            self._bump_ctag(target)
            self._bump_tok(target, href, "added")
            return new_etag
        # If-Match: <etag>
        if owner is None:
            raise FakeCalDAVNotFoundError(f"PUT If-Match on missing href {href}")
        if self._etags.get(href) != etag:
            raise FakeCalDAVConflictError(f"PUT If-Match etag mismatch for {href}")
        new_etag = self._next_etag(href)
        self._resources[owner][href] = ics
        self._etags[href] = new_etag
        self._bump_ctag(owner)
        self._bump_tok(owner, href, "changed")
        return new_etag

    def delete(self, href: str, etag: str) -> None:
        self.calls.append(("delete", href, etag))
        owner = self._owning_calendar(href)
        if owner is None:
            raise FakeCalDAVNotFoundError(f"DELETE on missing href {href}")
        if self._etags.get(href) != etag:
            raise FakeCalDAVConflictError(f"DELETE etag mismatch for {href}")
        del self._resources[owner][href]
        del self._etags[href]
        self._bump_ctag(owner)
        self._bump_tok(owner, href, "deleted")

    def sync_collection(
        self,
        calendar_url: str,
        sync_token: str,
    ) -> tuple[
        Sequence[tuple[str, str]],
        Sequence[str],
        str,
    ]:
        self.calls.append(("sync_collection", calendar_url, sync_token))
        if calendar_url not in self._tok_counters:
            raise FakeCalDAVNotFoundError(f"no calendar at {calendar_url}")
        if sync_token in self._tok_expired:
            raise SyncTokenExpiredError(f"sync-token expired: {sync_token}")
        # Parse counter from "tok-{n}" format.
        if not sync_token.startswith("tok-"):
            raise SyncTokenExpiredError(f"unrecognised sync-token format: {sync_token}")
        try:
            since = int(sync_token[4:])
        except ValueError as exc:
            raise SyncTokenExpiredError(
                f"unrecognised sync-token format: {sync_token}"
            ) from exc
        current = self._tok_counters[calendar_url]
        if since > current:
            raise SyncTokenExpiredError(
                f"sync-token {sync_token!r} is ahead of current counter {current}"
            )
        # Compute delta: replay log entries with counter > since.
        log = self._tok_log.get(calendar_url, [])
        delta: dict[str, str] = {}  # href → last action since `since`
        for counter, href, action in log:
            if counter > since:
                delta[href] = action
        changed: list[tuple[str, str]] = []
        deleted: list[str] = []
        for href, action in delta.items():
            if action == "deleted":
                deleted.append(href)
            else:
                etag = self._etags.get(href, "")
                changed.append((href, etag))
        new_token = f"tok-{current}"
        return tuple(changed), tuple(deleted), new_token

    def get_sync_token(self, calendar_url: str) -> str | None:
        self.calls.append(("get_sync_token", calendar_url))
        if calendar_url not in self._tok_counters:
            return None
        return f"tok-{self._tok_counters[calendar_url]}"

    def _next_etag(self, href: str) -> str:
        current = self._etags.get(href, "etag-0")
        head, _, tail = current.rpartition("-")
        try:
            return f"{head or 'etag'}-{int(tail) + 1}"
        except ValueError:
            return f"{current}+1"

    def _calendar_for_new_href(self, href: str) -> str | None:
        best: str | None = None
        for url in self._calendars:
            if href.startswith(url) and (best is None or len(url) > len(best)):
                best = url
        return best
