from __future__ import annotations

from collections.abc import Sequence

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
    """

    def __init__(self, *, principal_url: str = "/dav/principals/user/") -> None:
        self._principal_url = principal_url
        self._calendars: dict[str, RemoteCalendar] = {}
        self._resources: dict[str, dict[str, bytes]] = {}
        self._etags: dict[str, str] = {}
        self._ctags: dict[str, str] = {}
        self.calls: list[tuple[object, ...]] = []

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

    def put_resource(
        self, *, calendar_url: str, href: str, ics: bytes, etag: str
    ) -> None:
        self._resources[calendar_url][href] = ics
        self._etags[href] = etag
        self._bump_ctag(calendar_url)

    def remove_resource(self, calendar_url: str, href: str) -> None:
        self._resources[calendar_url].pop(href, None)
        self._etags.pop(href, None)
        self._bump_ctag(calendar_url)

    def set_ctag(self, calendar_url: str, ctag: str) -> None:
        self._ctags[calendar_url] = ctag

    def current_ctag(self, calendar_url: str) -> str | None:
        return self._ctags.get(calendar_url)

    def hrefs_in(self, calendar_url: str) -> tuple[str, ...]:
        return tuple(self._resources.get(calendar_url, {}).keys())

    def etag_for(self, href: str) -> str | None:
        return self._etags.get(href)

    def _bump_ctag(self, calendar_url: str) -> None:
        prev = self._ctags.get(calendar_url, "ctag-0")
        head, _, tail = prev.rpartition("-")
        try:
            self._ctags[calendar_url] = f"{head or 'ctag'}-{int(tail) + 1}"
        except ValueError:
            self._ctags[calendar_url] = f"{prev}+1"

    def _owning_calendar(self, href: str) -> str | None:
        for cal_url, resources in self._resources.items():
            if href in resources:
                return cal_url
        return None

    # CalDAVSession protocol --------------------------------------------------

    def discover_principal(self) -> str:
        self.calls.append(("discover_principal",))
        return self._principal_url

    def list_calendars(self, principal_url: str) -> Sequence[RemoteCalendar]:
        self.calls.append(("list_calendars", principal_url))
        return tuple(self._calendars.values())

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
