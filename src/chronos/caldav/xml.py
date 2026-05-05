"""XML body builders and multistatus parsers for CalDAV protocol."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from chronos.domain import ComponentKind, RemoteCalendar

# Namespaces
_DAV_NS = "DAV:"
_CALDAV_NS = "urn:ietf:params:xml:ns:caldav"
_CS_NS = "http://calendarserver.org/ns/"

# Sentinel etag for `calendar_query` results from servers that don't
# return `getetag` with the calendar-query REPORT.
_MISSING_SERVER_ETAG = ""

_MULTIGET_BATCH_SIZE = 100

# Register the namespaces once so `ET.tostring` emits the conventional
# `d:` / `c:` prefixes used by all CalDAV servers we've tested.
ET.register_namespace("d", _DAV_NS)
ET.register_namespace("c", _CALDAV_NS)

# ---- XML body constants ----------------------------------------------------

_CTAG_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<d:propfind xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">'
    "<d:prop><cs:getctag/></d:prop>"
    "</d:propfind>"
).encode("utf-8")

# calendar-query REPORT (RFC 4791 §7.8) asking only for `getetag`.
_CALENDAR_QUERY_BODY = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><d:getetag/></d:prop>"
    '<c:filter><c:comp-filter name="VCALENDAR"/></c:filter>'
    "</c:calendar-query>"
).encode("utf-8")

# Depth-1 PROPFIND against the calendar home-set.
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
).encode("utf-8")

_SYNC_TOKEN_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<d:propfind xmlns:d="DAV:">'
    "<d:prop><d:sync-token/></d:prop>"
    "</d:propfind>"
).encode("utf-8")

_CURRENT_USER_PRINCIPAL_BODY = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<d:propfind xmlns:d="DAV:">'
    "<d:prop><d:current-user-principal/></d:prop>"
    "</d:propfind>"
).encode("utf-8")

_CALENDAR_HOME_SET_BODY = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><c:calendar-home-set/></d:prop>"
    "</d:propfind>"
).encode("utf-8")


# ---- Body builders ---------------------------------------------------------


def _build_multiget_body(hrefs: Sequence[str]) -> bytes:
    """Build a calendar-multiget REPORT body (RFC 4791 §7.10).

    Hrefs already match the local-index form (URL-decoded path,
    absolute URL). Re-encode the path for the request body since the
    server expects percent-encoded hrefs.
    """
    root = ET.Element(f"{{{_CALDAV_NS}}}calendar-multiget")
    prop = ET.SubElement(root, f"{{{_DAV_NS}}}prop")
    ET.SubElement(prop, f"{{{_DAV_NS}}}getetag")
    ET.SubElement(prop, f"{{{_CALDAV_NS}}}calendar-data")
    for href in hrefs:
        elem = ET.SubElement(root, f"{{{_DAV_NS}}}href")
        elem.text = _path_for_request(href)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _build_sync_collection_body(sync_token: str) -> bytes:
    """Build a sync-collection REPORT body (RFC 6578 §3.2)."""
    root = ET.Element(f"{{{_DAV_NS}}}sync-collection")
    token_elem = ET.SubElement(root, f"{{{_DAV_NS}}}sync-token")
    token_elem.text = sync_token
    level_elem = ET.SubElement(root, f"{{{_DAV_NS}}}sync-level")
    level_elem.text = "1"
    prop = ET.SubElement(root, f"{{{_DAV_NS}}}prop")
    ET.SubElement(prop, f"{{{_DAV_NS}}}getetag")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


# ---- Parsers ---------------------------------------------------------------


def _parse_ctag(body: bytes) -> str | None:
    """Extract CalendarServer getctag from a PROPFIND depth-0 response."""
    if not body:
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


def _parse_calendar_query(
    body: bytes, *, base_url: str
) -> tuple[tuple[str, str], ...]:
    """Parse a calendar-query REPORT multistatus into (href, etag) pairs.

    Hrefs are returned as absolute URLs using `base_url`'s scheme+host
    to resolve relative paths.
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


def _parse_multiget(
    body: bytes, *, base_url: str
) -> list[tuple[str, str, bytes]]:
    """Parse a calendar-multiget REPORT multistatus into (href, etag, ics).

    Skips responses with only non-2xx propstat.
    Missing etags fall through to the content-hash etag.
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
                # from CRLF; iCalendar (RFC 5545 §3.1) requires CRLF.
                normalized = data_elem.text.replace("\r\n", "\n").replace(
                    "\n", "\r\n"
                )
                ics = normalized.encode("utf-8")
        if ics is None:
            continue
        if not etag:
            etag = _content_etag(ics)
        out.append((href, etag, ics))
    return out


def _parse_calendars_propfind(
    body: bytes,
    *,
    base_url: str,
) -> tuple[RemoteCalendar, ...]:
    """Parse a depth-1 PROPFIND response into RemoteCalendar objects.

    Only calendar collections are included; plain WebDAV collections
    (address-books, etc.) are skipped.
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


def _parse_sync_collection(
    body: bytes,
    *,
    base_url: str,
) -> tuple[list[tuple[str, str]], list[str], str | None]:
    """Parse a sync-collection REPORT multistatus (RFC 6578 §3.6).

    Returns `(changed, deleted, new_sync_token)`.
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


def _parse_current_user_principal(
    body: bytes, *, base_url: str
) -> str | None:
    """Extract `{DAV:}current-user-principal/{DAV:}href` from a PROPFIND response."""
    if not body:
        return None
    try:
        tree = ET.fromstring(body)
    except ET.ParseError:
        return None
    ns = {"d": _DAV_NS}
    # Look for current-user-principal/href in a 200 propstat
    for propstat in tree.findall(".//d:propstat", ns):
        status_elem = propstat.find("d:status", ns)
        if status_elem is None or " 200 " not in (status_elem.text or ""):
            continue
        href_elem = propstat.find(
            "d:prop/d:current-user-principal/d:href", ns
        )
        if href_elem is not None and href_elem.text:
            return _absolute_href(href_elem.text.strip(), base_url=base_url)
    return None


def _parse_calendar_home_set(
    body: bytes, *, base_url: str
) -> str | None:
    """Extract `{caldav}calendar-home-set/{DAV:}href` from a PROPFIND response."""
    if not body:
        return None
    try:
        tree = ET.fromstring(body)
    except ET.ParseError:
        return None
    ns = {"d": _DAV_NS, "c": _CALDAV_NS}
    for propstat in tree.findall(".//d:propstat", ns):
        status_elem = propstat.find("d:status", ns)
        if status_elem is None or " 200 " not in (status_elem.text or ""):
            continue
        href_elem = propstat.find(
            "d:prop/c:calendar-home-set/d:href", ns
        )
        if href_elem is not None and href_elem.text:
            return _absolute_href(href_elem.text.strip(), base_url=base_url)
    return None


# ---- Helpers ---------------------------------------------------------------


def _absolute_href(href: str, *, base_url: str) -> str:
    """Resolve `href` against `base_url` and URL-decode the path."""
    if not href:
        return href
    if href.startswith(("http://", "https://")):
        return unquote(href)
    parsed = urlsplit(base_url)
    return urlunsplit((parsed.scheme, parsed.netloc, unquote(href), "", ""))


def _chunk(items: Sequence[str], size: int) -> list[Sequence[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _content_etag(ics: bytes) -> str:
    """Stable per-content etag for servers that don't return getetag."""
    digest = hashlib.sha256(ics).hexdigest()[:32]
    return f'W/"chronos-{digest}"'


def _path_for_request(href: str) -> str:
    """Strip the scheme+host from `href` and percent-encode the path."""
    parsed = urlsplit(href)
    path = parsed.path if parsed.scheme and parsed.netloc else href
    return quote(path, safe="/:")


def _default_components() -> frozenset[ComponentKind]:
    return frozenset({ComponentKind.VEVENT, ComponentKind.VTODO})
