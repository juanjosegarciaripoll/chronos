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
import logging
from collections.abc import Sequence
from typing import Any, cast
from urllib.parse import quote, unquote, urlsplit, urlunsplit
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

logger = logging.getLogger(__name__)

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

# calendar-query REPORT (RFC 4791 §7.8) asking only for `getetag`.
# Deliberately omits `<C:calendar-data/>` so the server doesn't ship
# the entire ICS body in this round trip — `calendar_multiget` does
# that separately. This also avoids `caldav.Calendar.events()`'s
# fall-through to per-event GETs, which 404 on Google for
# recurrence-id override hrefs returned in the REPORT (those hrefs
# are listable but not directly GETtable).
_CALENDAR_QUERY_BODY = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><d:getetag/></d:prop>"
    '<c:filter><c:comp-filter name="VCALENDAR"/></c:filter>'
    "</c:calendar-query>"
)

_MULTIGET_BATCH_SIZE = 100

# Depth-1 PROPFIND against the calendar home-set that fetches everything
# needed to populate RemoteCalendar in a single round-trip: display name,
# resource type (to identify calendar collections), supported component set,
# CTag, and sync-token.  Replaces N per-calendar get_ctag() calls with one
# request at the start of every sync.
_CALENDARS_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<d:propfind xmlns:d="DAV:" '
    'xmlns:c="urn:ietf:params:xml:ns:caldav" '
    'xmlns:cs="http://calendarserver.org/ns/">'
    "<d:prop>"
    "<d:displayname/>"
    "<d:resourcetype/>"
    "<c:supported-calendar-component-set/>"
    "<cs:getctag/>"
    "<d:sync-token/>"
    "</d:prop>"
    "</d:propfind>"
)

_SYNC_TOKEN_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<d:propfind xmlns:d="DAV:">'
    "<d:prop><d:sync-token/></d:prop>"
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


class SyncTokenExpiredError(CalDAVError):
    """Raised when the server rejects a sync-token as invalid or expired.

    The sync engine catches this and falls back to the slow path, then
    re-acquires a fresh token via `get_sync_token`.
    """


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
        url = str(cast(object, principal.url))
        logger.info("discovered principal %s", url)
        return url

    def list_calendars(self, principal_url: str) -> Sequence[RemoteCalendar]:
        del principal_url  # caldav caches the principal across calls
        principal = self._get_principal()

        # Attempt a single depth-1 PROPFIND against the calendar home-set
        # that returns CTag and sync-token alongside the usual display name /
        # component-set.  This eliminates N per-calendar get_ctag() calls from
        # the sync loop.  Falls back to principal.calendars() if the home-set
        # URL is unavailable or the request fails.
        home_url = _calendar_home_url(principal)
        if home_url is not None:
            try:
                response = self._client.propfind(
                    home_url, props=_CALENDARS_PROPFIND_BODY, depth=1
                )
                result = _parse_calendars_propfind(
                    _response_body(response) or b"", base_url=home_url
                )
                if result:
                    logger.info("listed %d calendars (with state)", len(result))
                    return result
            except DAVError:
                pass  # fall through to the caldav-library path below

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
        logger.info("listed %d calendars", len(out))
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
        # Issue the calendar-query REPORT directly so we get only
        # (href, etag) pairs and never trigger a per-event GET. See the
        # `_CALENDAR_QUERY_BODY` comment for why `caldav.Calendar.events()`
        # is unsafe against Google.
        try:
            response = self._client.report(calendar_url, _CALENDAR_QUERY_BODY, depth=1)
        except NotFoundError as exc:
            raise CalDAVNotFoundError(str(exc)) from exc
        except DAVError as exc:
            raise CalDAVError(str(exc)) from exc
        pairs = _parse_calendar_query(
            _response_body(response) or b"", base_url=calendar_url
        )
        logger.info("  calendar-query: %d resource(s) listed by server", len(pairs))
        return pairs

    def calendar_multiget(
        self, calendar_url: str, hrefs: Sequence[str]
    ) -> Sequence[tuple[str, str, bytes]]:
        if not hrefs:
            return ()
        # Issue chunks of `_MULTIGET_BATCH_SIZE` to keep individual
        # response bodies manageable. RFC 4791 §7.10 places no bound
        # on the number of hrefs per multiget, but Google in practice
        # rejects very large bodies and a fresh "Holidays" calendar
        # can be 10k+ resources.
        out: list[tuple[str, str, bytes]] = []
        chunks = _chunk(hrefs, _MULTIGET_BATCH_SIZE)
        total = len(hrefs)
        # Per-chunk INFO logging (not just DEBUG) so big calendars
        # don't look stuck while a multi-thousand-resource fetch is
        # in flight. With ~5k events at 100/chunk that's 50 lines —
        # not noisy enough to bury other output.
        if len(chunks) > 1:
            logger.info(
                "fetching %d resources from %s (%d batches of up to %d)",
                total,
                calendar_url,
                len(chunks),
                _MULTIGET_BATCH_SIZE,
            )
        fetched = 0
        for batch_index, chunk in enumerate(chunks, start=1):
            body = _build_multiget_body(chunk)
            try:
                response = self._client.report(calendar_url, body, depth=1)
            except NotFoundError as exc:
                raise CalDAVNotFoundError(str(exc)) from exc
            except DAVError as exc:
                raise CalDAVError(str(exc)) from exc
            parsed = _parse_multiget(
                _response_body(response) or b"", base_url=calendar_url
            )
            out.extend(parsed)
            fetched += len(chunk)
            if len(chunks) > 1:
                logger.info(
                    "  batch %d/%d: %d/%d resources fetched",
                    batch_index,
                    len(chunks),
                    fetched,
                    total,
                )
        logger.debug(
            "REPORT calendar-multiget %s -> %d body(ies)", calendar_url, len(out)
        )
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

    def sync_collection(
        self,
        calendar_url: str,
        sync_token: str,
    ) -> tuple[
        Sequence[tuple[str, str]],
        Sequence[str],
        str,
    ]:
        """Issue an RFC 6578 sync-collection REPORT and return the delta.

        Returns `(changed, deleted, new_sync_token)` where `changed` is
        a sequence of `(href, etag)` for added or modified resources and
        `deleted` is a sequence of hrefs removed on the server.

        Raises `SyncTokenExpiredError` when the server signals that the
        token is invalid (403 / 409 with `valid-sync-token` condition),
        which the sync engine catches to fall back to the slow path.
        """
        body = _build_sync_collection_body(sync_token)
        try:
            response = self._client.report(calendar_url, body, depth=1)
        except AuthorizationError as exc:
            raise SyncTokenExpiredError(
                f"sync-collection {calendar_url}: token rejected (403): {exc}"
            ) from exc
        except NotFoundError as exc:
            raise CalDAVNotFoundError(str(exc)) from exc
        except DAVError as exc:
            err = str(exc)
            if "409" in err:
                raise SyncTokenExpiredError(
                    f"sync-collection {calendar_url}: token invalid (409): {exc}"
                ) from exc
            raise CalDAVError(err) from exc
        raw = _response_body(response) or b""
        changed, deleted, new_token = _parse_sync_collection(raw, base_url=calendar_url)
        if new_token is None:
            raise SyncTokenExpiredError(
                f"sync-collection {calendar_url}: response contained no sync-token"
            )
        logger.info(
            "  sync-collection: %d changed, %d deleted",
            len(changed),
            len(deleted),
        )
        return tuple(changed), tuple(deleted), new_token

    def get_sync_token(self, calendar_url: str) -> str | None:
        """Fetch the current `DAV:sync-token` for `calendar_url`.

        Returns the opaque token string, or `None` when the server does
        not expose the property (servers without sync-collection support
        return an empty or absent propstat).
        """
        try:
            response = self._client.propfind(
                calendar_url, props=_SYNC_TOKEN_PROPFIND_BODY, depth=0
            )
        except DAVError:
            return None
        return _parse_sync_token_propfind(_response_body(response) or b"")

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


def _parse_calendar_query(body: bytes, *, base_url: str) -> tuple[tuple[str, str], ...]:
    """Parse a calendar-query REPORT multistatus into (href, etag) pairs.

    Hrefs are returned as absolute URLs (some servers, including
    Google, return relative paths like `/caldav/v2/.../foo.ics`),
    using `base_url`'s scheme+host to resolve them. The rest of the
    sync engine compares hrefs as opaque strings, so consistency
    matters more than correctness of any specific format.

    Falls back to the empty-string sentinel etag when a propstat
    carried `getetag` with a non-2xx status — Google embeds a 404
    propstat for properties it doesn't expose on a given resource,
    and the multiget pass derives a content-hash etag in that case.
    """
    if not body:
        return ()
    try:
        tree = ET.fromstring(body)
    except ET.ParseError:
        return ()
    ns = {"d": _DAV_NS}
    out: list[tuple[str, str]] = []
    for response in tree.findall("d:response", ns):
        href_elem = response.find("d:href", ns)
        if href_elem is None or href_elem.text is None:
            continue
        href = _absolute_href(href_elem.text.strip(), base_url=base_url)
        if not href:
            continue
        etag = ""
        for propstat in response.findall("d:propstat", ns):
            status_elem = propstat.find("d:status", ns)
            if status_elem is None or status_elem.text is None:
                continue
            if " 200 " not in status_elem.text:
                continue
            etag_elem = propstat.find("d:prop/d:getetag", ns)
            if etag_elem is not None and etag_elem.text:
                etag = etag_elem.text.strip().strip('"')
                break
        out.append((href, etag or _MISSING_SERVER_ETAG))
    return tuple(out)


def _absolute_href(href: str, *, base_url: str) -> str:
    """Resolve `href` against `base_url` and URL-decode the path.

    URL-decoding matches what `caldav.Event.url` did before the rewrite
    to raw REPORT (`%40` → `@`). Existing local-index rows were stored
    in that decoded form, so without this normalization a fresh
    `calendar_query` returns hrefs that don't match local hrefs and
    sync mistakes everything for "deleted on server" — tripping the
    mass-deletion guard.
    """
    if not href:
        return href
    if href.startswith(("http://", "https://")):
        return unquote(href)
    parsed = urlsplit(base_url)
    return urlunsplit((parsed.scheme, parsed.netloc, unquote(href), "", ""))


def _chunk(items: Sequence[str], size: int) -> list[Sequence[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


_CALDAV_NS = "urn:ietf:params:xml:ns:caldav"

# Register the namespaces once so `ET.tostring` emits the conventional
# `d:` / `c:` prefixes used by all CalDAV servers we've tested.
ET.register_namespace("d", _DAV_NS)
ET.register_namespace("c", _CALDAV_NS)


def _build_multiget_body(hrefs: Sequence[str]) -> str:
    """Build a calendar-multiget REPORT body (RFC 4791 §7.10).

    Hrefs already match the local-index form (URL-decoded path,
    absolute URL). Re-encode the path for the request body since the
    server expects percent-encoded hrefs (Google's responses use
    `%40` for `@`, and rejects the bare-`@` form in some multiget
    bodies). Built via `xml.etree.ElementTree` so escaping and
    encoding are the stdlib's responsibility, not ours.
    """
    root = ET.Element(f"{{{_CALDAV_NS}}}calendar-multiget")
    prop = ET.SubElement(root, f"{{{_DAV_NS}}}prop")
    ET.SubElement(prop, f"{{{_DAV_NS}}}getetag")
    ET.SubElement(prop, f"{{{_CALDAV_NS}}}calendar-data")
    for href in hrefs:
        elem = ET.SubElement(root, f"{{{_DAV_NS}}}href")
        elem.text = _path_for_request(href)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _path_for_request(href: str) -> str:
    """Strip the scheme+host from `href` and percent-encode the path."""
    parsed = urlsplit(href)
    path = parsed.path if parsed.scheme and parsed.netloc else href
    # `safe="/:"` keeps path separators and the colon literal; every
    # other character (including `@`) is percent-encoded.
    return quote(path, safe="/:")


def _parse_multiget(body: bytes, *, base_url: str) -> list[tuple[str, str, bytes]]:
    """Parse a calendar-multiget REPORT multistatus into (href, etag, ics).

    Skips responses that carried only a non-2xx propstat — those are
    hrefs the server reported in calendar-query but can't actually
    expose to multiget (notably Google recurrence-id override URLs).
    Missing etags fall through to the content-hash etag in
    `_content_etag`, matching the calendar-query path.
    """
    if not body:
        return []
    try:
        tree = ET.fromstring(body)
    except ET.ParseError:
        return []
    ns = {"d": _DAV_NS, "c": "urn:ietf:params:xml:ns:caldav"}
    out: list[tuple[str, str, bytes]] = []
    for response in tree.findall("d:response", ns):
        href_elem = response.find("d:href", ns)
        if href_elem is None or href_elem.text is None:
            continue
        href = _absolute_href(href_elem.text.strip(), base_url=base_url)
        if not href:
            continue
        etag = ""
        ics: bytes | None = None
        for propstat in response.findall("d:propstat", ns):
            status_elem = propstat.find("d:status", ns)
            if status_elem is None or status_elem.text is None:
                continue
            if " 200 " not in status_elem.text:
                continue
            etag_elem = propstat.find("d:prop/d:getetag", ns)
            if etag_elem is not None and etag_elem.text:
                etag = etag_elem.text.strip().strip('"')
            data_elem = propstat.find("d:prop/c:calendar-data", ns)
            if data_elem is not None and data_elem.text:
                # XML 1.0 §2.11 line-ending normalization strips CR
                # from CRLF inside text content; iCalendar (RFC 5545
                # §3.1) requires CRLF, so restore it.
                normalized = data_elem.text.replace("\r\n", "\n").replace("\n", "\r\n")
                ics = normalized.encode("utf-8")
        if ics is None:
            continue
        if not etag:
            etag = _content_etag(ics)
        out.append((href, etag, ics))
    return out


def _calendar_home_url(principal: Any) -> str | None:
    """Return the first calendar home-set URL from a caldav principal object.

    Reads from the already-fetched principal properties (no extra network
    request).  Returns None when the attribute is absent or the URL cannot
    be determined so the caller can fall back to `principal.calendars()`.
    Only URLs that begin with "http://" or "https://" are accepted so that
    mock objects and malformed values are safely rejected.
    """
    try:
        home_sets = cast(list[Any], principal.calendar_home_set)
        if not home_sets:
            return None
        url = str(cast(object, home_sets[0].url))
        return url if url.startswith(("http://", "https://")) else None
    except (AttributeError, IndexError, DAVError, TypeError, ValueError):
        return None


def _parse_calendars_propfind(
    body: bytes,
    *,
    base_url: str,
) -> tuple[RemoteCalendar, ...]:
    """Parse a depth-1 PROPFIND response into RemoteCalendar objects.

    Only responses whose resourcetype contains the CalDAV `calendar` element
    are included; plain WebDAV collections (address-books, etc.) are skipped.
    Populates `ctag` and `sync_token` when the server returns them.
    """
    if not body:
        return ()
    try:
        tree = ET.fromstring(body)
    except ET.ParseError:
        return ()
    ns = {"d": _DAV_NS, "c": _CALDAV_NS, "cs": _CS_NS}
    out: list[RemoteCalendar] = []
    for response in tree.findall("d:response", ns):
        href_elem = response.find("d:href", ns)
        if href_elem is None or href_elem.text is None:
            continue
        href = _absolute_href(href_elem.text.strip(), base_url=base_url)
        if not href:
            continue
        is_calendar = False
        name: str | None = None
        supported: frozenset[ComponentKind] = frozenset()
        ctag: str | None = None
        sync_token: str | None = None
        for propstat in response.findall("d:propstat", ns):
            status_elem = propstat.find("d:status", ns)
            if status_elem is None or " 200 " not in (status_elem.text or ""):
                continue
            prop = propstat.find("d:prop", ns)
            if prop is None:
                continue
            rt = prop.find("d:resourcetype", ns)
            if rt is not None and rt.find("c:calendar", ns) is not None:
                is_calendar = True
            dn = prop.find("d:displayname", ns)
            if dn is not None and dn.text:
                name = dn.text.strip()
            sccs = prop.find("c:supported-calendar-component-set", ns)
            if sccs is not None:
                kinds: set[ComponentKind] = set()
                for comp in sccs:
                    if comp.tag == f"{{{_CALDAV_NS}}}comp":
                        cname = comp.get("name", "").upper()
                        if cname == "VEVENT":
                            kinds.add(ComponentKind.VEVENT)
                        elif cname == "VTODO":
                            kinds.add(ComponentKind.VTODO)
                if kinds:
                    supported = frozenset(kinds)
            ct = prop.find("cs:getctag", ns)
            if ct is not None and ct.text:
                ctag = ct.text.strip() or None
            st = prop.find("d:sync-token", ns)
            if st is not None and st.text:
                sync_token = st.text.strip() or None
        if not is_calendar:
            continue
        out.append(
            RemoteCalendar(
                name=name or href.rstrip("/").rsplit("/", 1)[-1] or href,
                url=href,
                supported_components=supported if supported else _default_components(),
                ctag=ctag,
                sync_token=sync_token,
            )
        )
    return tuple(out)


def _build_sync_collection_body(sync_token: str) -> str:
    """Build a sync-collection REPORT body (RFC 6578 §3.2)."""
    root = ET.Element(f"{{{_DAV_NS}}}sync-collection")
    token_elem = ET.SubElement(root, f"{{{_DAV_NS}}}sync-token")
    token_elem.text = sync_token
    level_elem = ET.SubElement(root, f"{{{_DAV_NS}}}sync-level")
    level_elem.text = "1"
    prop = ET.SubElement(root, f"{{{_DAV_NS}}}prop")
    ET.SubElement(prop, f"{{{_DAV_NS}}}getetag")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _parse_sync_collection(
    body: bytes,
    *,
    base_url: str,
) -> tuple[list[tuple[str, str]], list[str], str | None]:
    """Parse a sync-collection REPORT multistatus (RFC 6578 §3.6).

    Returns `(changed, deleted, new_sync_token)`:
    - `changed`: `(href, etag)` pairs for resources added or modified.
    - `deleted`: hrefs for resources removed on the server (404 propstat).
    - `new_sync_token`: the `<d:sync-token>` element from the multistatus
      root, or `None` if the server omitted it.
    """
    if not body:
        return [], [], None
    try:
        tree = ET.fromstring(body)
    except ET.ParseError:
        return [], [], None
    ns = {"d": _DAV_NS}
    changed: list[tuple[str, str]] = []
    deleted: list[str] = []
    for response in tree.findall("d:response", ns):
        href_elem = response.find("d:href", ns)
        if href_elem is None or href_elem.text is None:
            continue
        href = _absolute_href(href_elem.text.strip(), base_url=base_url)
        if not href:
            continue
        is_deleted = False
        etag = ""
        for propstat in response.findall("d:propstat", ns):
            status_elem = propstat.find("d:status", ns)
            if status_elem is None or status_elem.text is None:
                continue
            if " 404 " in status_elem.text:
                is_deleted = True
                break
            if " 200 " in status_elem.text:
                etag_elem = propstat.find("d:prop/d:getetag", ns)
                if etag_elem is not None and etag_elem.text:
                    etag = etag_elem.text.strip().strip('"')
        if is_deleted:
            deleted.append(href)
        else:
            changed.append((href, etag or _MISSING_SERVER_ETAG))
    token_elem = tree.find("d:sync-token", ns)
    new_token: str | None = None
    if token_elem is not None and token_elem.text:
        new_token = token_elem.text.strip() or None
    return changed, deleted, new_token


def _parse_sync_token_propfind(body: bytes) -> str | None:
    """Extract `DAV:sync-token` from a PROPFIND depth-0 response."""
    if not body:
        return None
    try:
        tree = ET.fromstring(body)
    except ET.ParseError:
        return None
    ns = {"d": _DAV_NS}
    elem = tree.find(".//d:sync-token", ns)
    if elem is None or not elem.text:
        return None
    value = elem.text.strip()
    return value or None


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
    "SyncTokenExpiredError",
]
