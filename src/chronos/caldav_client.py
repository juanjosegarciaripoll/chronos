"""HTTP-backed CalDAVSession implementation over the `caldav` library.

Satisfies the `CalDAVSession` Protocol in `chronos.protocols`. The sync
engine calls Protocol methods; this module bridges them to the concrete
`caldav` library. Every network-facing call translates
`caldav.lib.error.*` exceptions into the `CalDAVError` hierarchy defined
here so callers never see `caldav.*` types.

Known v1 limitations:

- **Conditional DELETE**: caldav 3.1's `DAVClient.delete(url)` takes no
  `headers` parameter, so `If-Match` can't be sent at the public API
  level. We issue an unconditional DELETE; the sync engine's etag
  reconciliation on the next pass catches rare server-side races.

- **CTag PROPFIND**: the CalendarServer CTag property is not exposed
  through a first-class class in `caldav.elements`. We issue a raw
  PROPFIND with an XML body and parse the response. If the server
  doesn't return a CTag (or parsing fails), the session reports `None`
  and the sync engine falls through to the slow path — correct, just
  slower.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any, cast
from xml.etree import ElementTree as ET

import caldav
from caldav.lib.error import (
    AuthorizationError,
    DAVError,
    NotFoundError,
    PutError,
)

from chronos.authorization import Authorization
from chronos.domain import ComponentKind, RemoteCalendar

# Sentinel etag for `calendar_query` results from servers that don't
# return `getetag` with the calendar-query REPORT (some Exchange-style
# CalDAV gateways do this). The sync engine treats it as "different
# from any local etag", forcing a multiget; the multiget falls back to
# a content-hash etag (`_content_etag`) so subsequent change detection
# still works.
_MISSING_SERVER_ETAG = ""

_DAV_NS = "DAV:"
_CS_NS = "http://calendarserver.org/ns/"

_CTAG_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<d:propfind xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">'
    "<d:prop><cs:getctag/></d:prop>"
    "</d:propfind>"
)


class CalDAVError(Exception):
    pass


class CalDAVConflictError(CalDAVError):
    """Raised on 412 Precondition Failed (etag mismatch)."""


class CalDAVNotFoundError(CalDAVError):
    """Raised on 404."""


class CalDAVAuthError(CalDAVError):
    """Raised on 401 / 403."""


class CalDAVHttpSession:
    def __init__(self, *, url: str, authorization: Authorization) -> None:
        # `caldav.DAVClient` has partial type stubs; keep the library's
        # object behind `Any` inside this module and translate at the
        # Protocol boundary.
        self._client: Any = _build_client(url, authorization)
        self._principal: Any = None
        self._calendar_cache: dict[str, Any] = {}

    def discover_principal(self) -> str:
        principal = self._get_principal()
        return str(cast(object, principal.url))

    def list_calendars(self, principal_url: str) -> Sequence[RemoteCalendar]:
        del principal_url  # caldav caches the principal across calls
        principal = self._get_principal()
        try:
            calendars: list[Any] = list(principal.calendars())
        except DAVError as exc:
            raise CalDAVError(str(exc)) from exc
        out: list[RemoteCalendar] = []
        for cal in calendars:
            cal_url = str(cast(object, cal.url))
            self._calendar_cache[cal_url] = cal
            out.append(
                RemoteCalendar(
                    name=_extract_name(cal, fallback_url=cal_url),
                    url=cal_url,
                    supported_components=_extract_supported_components(cal),
                )
            )
        return tuple(out)

    def get_ctag(self, calendar_url: str) -> str | None:
        try:
            response = self._client.propfind(
                calendar_url, props=_CTAG_PROPFIND_BODY, depth=0
            )
        except NotFoundError as exc:
            raise CalDAVNotFoundError(str(exc)) from exc
        except DAVError as exc:
            raise CalDAVError(str(exc)) from exc
        return _parse_ctag(response)

    def calendar_query(self, calendar_url: str) -> Sequence[tuple[str, str]]:
        calendar = self._find_calendar(calendar_url)
        try:
            events: list[Any] = list(calendar.events())
        except NotFoundError as exc:
            raise CalDAVNotFoundError(str(exc)) from exc
        except DAVError as exc:
            raise CalDAVError(str(exc)) from exc
        out: list[tuple[str, str]] = []
        for event in events:
            href = _opt_str(cast(object, getattr(event, "url", None)))
            if href is None:
                continue
            etag = _opt_str(cast(object, getattr(event, "etag", None)))
            # Some servers omit getetag in the calendar-query REPORT.
            # Use a sentinel so the event isn't silently dropped; the
            # multiget pass will compute a stable content-hash etag.
            out.append((href, etag or _MISSING_SERVER_ETAG))
        return tuple(out)

    def calendar_multiget(
        self, calendar_url: str, hrefs: Sequence[str]
    ) -> Sequence[tuple[str, str, bytes]]:
        calendar = self._find_calendar(calendar_url)
        out: list[tuple[str, str, bytes]] = []
        for href in hrefs:
            try:
                event = calendar.event_by_url(href)
            except NotFoundError:
                continue
            except DAVError as exc:
                raise CalDAVError(str(exc)) from exc
            ics = _extract_ics(event)
            if ics is None:
                continue
            resolved_href = _opt_str(cast(object, getattr(event, "url", href))) or href
            etag = _opt_str(cast(object, getattr(event, "etag", None)))
            if not etag:
                # Server didn't return getetag; derive a stable
                # per-content etag from the body so the next sync's
                # change detection still has something to compare.
                etag = _content_etag(ics)
            out.append((resolved_href, etag, ics))
        return tuple(out)

    def put(self, href: str, ics: bytes, etag: str | None) -> str:
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if etag is None:
            headers["If-None-Match"] = "*"
        else:
            headers["If-Match"] = etag
        try:
            body = ics.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CalDAVError(f"PUT {href}: body is not UTF-8") from exc
        try:
            response = self._client.put(href, body, headers)
        except PutError as exc:
            raise _translate_write_error(exc, href) from exc
        except DAVError as exc:
            raise CalDAVError(f"PUT {href}: {exc}") from exc
        return _extract_response_etag(response) or ""

    def delete(self, href: str, etag: str) -> None:
        # See module docstring: If-Match is not supported at the public
        # DAVClient.delete API level in caldav 3.1. Best-effort DELETE;
        # the sync engine's etag reconciliation catches races next pass.
        del etag
        try:
            self._client.delete(href)
        except NotFoundError as exc:
            raise CalDAVNotFoundError(f"DELETE {href}: {exc}") from exc
        except DAVError as exc:
            raise CalDAVError(f"DELETE {href}: {exc}") from exc

    def _get_principal(self) -> Any:
        if self._principal is None:
            try:
                self._principal = self._client.principal()
            except AuthorizationError as exc:
                raise CalDAVAuthError(str(exc)) from exc
            except DAVError as exc:
                raise CalDAVError(str(exc)) from exc
        return self._principal

    def _find_calendar(self, calendar_url: str) -> Any:
        cached = self._calendar_cache.get(calendar_url)
        if cached is not None:
            return cached
        principal = self._get_principal()
        try:
            calendars: list[Any] = list(principal.calendars())
        except DAVError as exc:
            raise CalDAVError(str(exc)) from exc
        for calendar in calendars:
            url = str(cast(object, calendar.url))
            self._calendar_cache[url] = calendar
            if url == calendar_url:
                return calendar
        raise CalDAVNotFoundError(f"calendar not found: {calendar_url}")


# Helpers ---------------------------------------------------------------------


def _extract_name(calendar: Any, *, fallback_url: str) -> str:
    try:
        display_name = cast(object, calendar.get_display_name())
    except DAVError:
        display_name = None
    except AttributeError:
        display_name = None
    if display_name:
        return str(display_name)
    name_attr = cast(object, getattr(calendar, "name", None))
    if name_attr:
        return str(name_attr)
    return fallback_url.rstrip("/").rsplit("/", 1)[-1] or fallback_url


def _extract_supported_components(calendar: Any) -> frozenset[ComponentKind]:
    try:
        raw = cast(object, calendar.get_supported_components())
    except DAVError:
        return _default_components()
    except AttributeError:
        return _default_components()
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return _default_components()
    items: list[object] = list(cast(list[object], raw))
    kinds: set[ComponentKind] = set()
    for item in items:
        name = str(item).upper()
        if name == "VEVENT":
            kinds.add(ComponentKind.VEVENT)
        elif name == "VTODO":
            kinds.add(ComponentKind.VTODO)
    if not kinds:
        return _default_components()
    return frozenset(kinds)


def _default_components() -> frozenset[ComponentKind]:
    return frozenset({ComponentKind.VEVENT, ComponentKind.VTODO})


def _parse_ctag(response: Any) -> str | None:
    body = _response_body(response)
    if body is None:
        return None
    try:
        tree = ET.fromstring(body)
    except ET.ParseError:
        return None
    ns = {"d": _DAV_NS, "cs": _CS_NS}
    elem = tree.find(".//cs:getctag", ns)
    if elem is None or elem.text is None:
        return None
    value = elem.text.strip()
    return value or None


def _response_body(response: Any) -> bytes | None:
    # caldav's DAVResponse exposes the raw body under `.raw` or `.tree`;
    # version to version this varies, so probe a few candidates.
    for attr in ("raw", "content", "body"):
        value = cast(object, getattr(response, attr, None))
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode("utf-8")
    # Some DAVResponse instances hold a parsed tree under `.tree`.
    tree = cast(object, getattr(response, "tree", None))
    if tree is not None:
        try:
            serialised = ET.tostring(cast(ET.Element, tree), encoding="utf-8")
        except (ET.ParseError, TypeError):
            return None
        return cast(bytes, serialised)
    return None


def _extract_ics(event: Any) -> bytes | None:
    data = cast(object, getattr(event, "data", None))
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8")
    return None


def _extract_response_etag(response: Any) -> str | None:
    headers = cast(object, getattr(response, "headers", None))
    if headers is None:
        return None
    # `requests.Response.headers` is case-insensitive; support both forms
    # plus plain mapping fallback.
    for key in ("ETag", "etag"):
        getter = getattr(headers, "get", None)
        if getter is None:
            continue
        value = cast(object, getter(key))
        if value is None:
            continue
        if isinstance(value, (str, bytes)):
            text = value.decode("utf-8") if isinstance(value, bytes) else value
            return text.strip().strip('"')
    return None


def _translate_write_error(exc: PutError, href: str) -> CalDAVError:
    message = str(exc)
    if "412" in message:
        return CalDAVConflictError(f"PUT {href}: precondition failed ({exc})")
    if "404" in message:
        return CalDAVNotFoundError(f"PUT {href}: {exc}")
    return CalDAVError(f"PUT {href}: {exc}")


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _content_etag(ics: bytes) -> str:
    """Stable per-content etag for servers that don't return getetag.

    Marked as a WebDAV weak validator (`W/`) to make it visually
    obvious in logs that this isn't a server-issued value, and to
    avoid colliding with any hex string a real server might mint.
    """
    digest = hashlib.sha256(ics).hexdigest()[:32]
    return f'W/"chronos-{digest}"'


def _build_client(url: str, authorization: Authorization) -> Any:
    if authorization.basic is not None:
        username, password = authorization.basic
        return caldav.DAVClient(  # type: ignore[operator]
            url=url, username=username, password=password
        )
    if authorization.http_auth is not None:
        return caldav.DAVClient(  # type: ignore[operator]
            url=url, auth=authorization.http_auth
        )
    raise CalDAVError("Authorization has neither `basic` nor `http_auth` set")


__all__ = [
    "CalDAVAuthError",
    "CalDAVConflictError",
    "CalDAVError",
    "CalDAVHttpSession",
    "CalDAVNotFoundError",
]
