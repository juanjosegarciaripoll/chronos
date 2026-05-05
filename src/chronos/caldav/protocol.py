"""Low-level CalDAV protocol functions.

Each function takes a `Client` instance and issues one or more HTTP
requests, translating HTTP errors into CalDAV-specific exceptions.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from urllib.parse import urlsplit

from chronos.caldav.errors import (
    CalDAVAuthError,
    CalDAVConflictError,
    CalDAVError,
    CalDAVNotFoundError,
    SyncTokenExpiredError,
)
from chronos.caldav.xml import (
    _CALENDAR_HOME_SET_BODY,
    _CALENDAR_QUERY_BODY,
    _CALENDARS_PROPFIND_BODY,
    _CTAG_PROPFIND_BODY,
    _CURRENT_USER_PRINCIPAL_BODY,
    _MULTIGET_BATCH_SIZE,
    _SYNC_TOKEN_PROPFIND_BODY,
    _build_multiget_body,
    _build_sync_collection_body,
    _chunk,
    _parse_calendar_home_set,
    _parse_calendar_query,
    _parse_calendars_propfind,
    _parse_ctag,
    _parse_current_user_principal,
    _parse_multiget,
    _parse_sync_collection,
    _parse_sync_token_propfind,
)
from chronos.domain import RemoteCalendar
from chronos.http import Client, HttpStatusError

logger = logging.getLogger(__name__)

_XML_CONTENT_TYPE = {"Content-Type": "application/xml"}


def _propfind_headers(depth: str) -> dict[str, str]:
    return {"Content-Type": "application/xml", "Depth": depth}


def discover_principal(client: Client, base_path: str = "/") -> str:
    """PROPFIND for {DAV:}current-user-principal; return the principal URL."""
    try:
        resp = client.request(
            "PROPFIND",
            base_path,
            body=_CURRENT_USER_PRINCIPAL_BODY,
            headers=_propfind_headers("0"),
        )
    except HttpStatusError as exc:
        if exc.status in (401, 403):
            raise CalDAVAuthError(
                f"authentication required at {base_path}"
            ) from exc
        if exc.status == 404:
            raise CalDAVNotFoundError(
                f"principal not found at {base_path}"
            ) from exc
        raise CalDAVError(f"PROPFIND {base_path}: HTTP {exc.status}") from exc

    principal = _parse_current_user_principal(resp.body, base_url=_client_base_url(client, base_path))
    if principal is None:
        # Server doesn't support current-user-principal; return path as-is
        return base_path
    logger.info("discovered principal %s", principal)
    return principal


def get_calendar_home_set(client: Client, principal_url: str) -> str:
    """PROPFIND on principal URL for calendar-home-set."""
    path = urlsplit(principal_url).path or "/"
    base_url = _client_base_url(client, path)
    try:
        resp = client.request(
            "PROPFIND",
            path,
            body=_CALENDAR_HOME_SET_BODY,
            headers=_propfind_headers("0"),
        )
    except HttpStatusError as exc:
        if exc.status in (401, 403):
            raise CalDAVAuthError(
                f"authentication required at {path}"
            ) from exc
        if exc.status == 404:
            raise CalDAVNotFoundError(f"not found: {path}") from exc
        raise CalDAVError(f"PROPFIND {path}: HTTP {exc.status}") from exc

    home_set = _parse_calendar_home_set(resp.body, base_url=base_url)
    if home_set is None:
        return principal_url
    return home_set


def list_calendars(
    client: Client, home_set_url: str
) -> tuple[RemoteCalendar, ...]:
    """PROPFIND Depth:1 on home-set; return RemoteCalendar for each calendar."""
    path = urlsplit(home_set_url).path or "/"
    base_url = _client_base_url(client, path)
    try:
        resp = client.request(
            "PROPFIND",
            path,
            body=_CALENDARS_PROPFIND_BODY,
            headers=_propfind_headers("1"),
        )
    except HttpStatusError as exc:
        if exc.status in (401, 403):
            raise CalDAVAuthError(
                f"authentication required at {path}"
            ) from exc
        if exc.status == 404:
            raise CalDAVNotFoundError(f"not found: {path}") from exc
        raise CalDAVError(f"PROPFIND {path}: HTTP {exc.status}") from exc

    calendars = _parse_calendars_propfind(resp.body, base_url=base_url)
    logger.info("listed %d calendars", len(calendars))
    return calendars


def get_ctag(client: Client, calendar_url: str) -> str | None:
    """PROPFIND for {CalendarServer}getctag."""
    path = urlsplit(calendar_url).path or "/"
    try:
        resp = client.request(
            "PROPFIND",
            path,
            body=_CTAG_PROPFIND_BODY,
            headers=_propfind_headers("0"),
        )
    except HttpStatusError as exc:
        if exc.status == 404:
            raise CalDAVNotFoundError(f"not found: {path}") from exc
        if exc.status in (401, 403):
            raise CalDAVAuthError(f"authentication required at {path}") from exc
        raise CalDAVError(f"PROPFIND {path}: HTTP {exc.status}") from exc

    return _parse_ctag(resp.body)


def get_sync_token(client: Client, calendar_url: str) -> str | None:
    """PROPFIND for {DAV:}sync-token."""
    path = urlsplit(calendar_url).path or "/"
    try:
        resp = client.request(
            "PROPFIND",
            path,
            body=_SYNC_TOKEN_PROPFIND_BODY,
            headers=_propfind_headers("0"),
        )
    except HttpStatusError:
        return None

    return _parse_sync_token_propfind(resp.body)


def calendar_query(
    client: Client, calendar_url: str
) -> tuple[tuple[str, str], ...]:
    """REPORT calendar-query (etags only)."""
    path = urlsplit(calendar_url).path or "/"
    try:
        resp = client.request(
            "REPORT",
            path,
            body=_CALENDAR_QUERY_BODY,
            headers=_propfind_headers("1"),
        )
    except HttpStatusError as exc:
        if exc.status == 404:
            raise CalDAVNotFoundError(f"not found: {path}") from exc
        if exc.status in (401, 403):
            raise CalDAVAuthError(f"authentication required at {path}") from exc
        raise CalDAVError(f"REPORT {path}: HTTP {exc.status}") from exc

    pairs = _parse_calendar_query(resp.body, base_url=calendar_url)
    logger.info("  calendar-query: %d resource(s) listed by server", len(pairs))
    return pairs


def calendar_multiget(
    client: Client,
    calendar_url: str,
    hrefs: Sequence[str],
) -> list[tuple[str, str, bytes]]:
    """REPORT calendar-multiget in batches."""
    if not hrefs:
        return []
    path = urlsplit(calendar_url).path or "/"
    chunks = _chunk(list(hrefs), _MULTIGET_BATCH_SIZE)
    total = len(hrefs)
    if len(chunks) > 1:
        logger.info(
            "fetching %d resources from %s (%d batches of up to %d)",
            total,
            calendar_url,
            len(chunks),
            _MULTIGET_BATCH_SIZE,
        )
    out: list[tuple[str, str, bytes]] = []
    fetched = 0
    for batch_index, chunk in enumerate(chunks, start=1):
        body = _build_multiget_body(chunk)
        try:
            resp = client.request(
                "REPORT",
                path,
                body=body,
                headers=_propfind_headers("1"),
            )
        except HttpStatusError as exc:
            if exc.status == 404:
                raise CalDAVNotFoundError(f"not found: {path}") from exc
            if exc.status in (401, 403):
                raise CalDAVAuthError(
                    f"authentication required at {path}"
                ) from exc
            raise CalDAVError(f"REPORT {path}: HTTP {exc.status}") from exc
        parsed = _parse_multiget(resp.body, base_url=calendar_url)
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
    return out


def sync_collection(
    client: Client,
    calendar_url: str,
    sync_token: str,
) -> tuple[list[tuple[str, str]], list[str], str | None]:
    """REPORT sync-collection."""
    path = urlsplit(calendar_url).path or "/"
    body = _build_sync_collection_body(sync_token)
    try:
        resp = client.request(
            "REPORT",
            path,
            body=body,
            headers=_propfind_headers("1"),
        )
    except HttpStatusError as exc:
        if exc.status == 403:
            raise SyncTokenExpiredError(
                f"sync-collection {calendar_url}: token rejected (403)"
            ) from exc
        if exc.status == 409:
            raise SyncTokenExpiredError(
                f"sync-collection {calendar_url}: token invalid (409)"
            ) from exc
        if exc.status == 404:
            raise CalDAVNotFoundError(f"not found: {path}") from exc
        if exc.status in (401,):
            raise CalDAVAuthError(
                f"authentication required at {path}"
            ) from exc
        # Check if 409 might be embedded in body or status code string
        err = str(exc)
        if "409" in err:
            raise SyncTokenExpiredError(
                f"sync-collection {calendar_url}: token invalid (409): {exc}"
            ) from exc
        raise CalDAVError(f"REPORT {path}: HTTP {exc.status}") from exc

    changed, deleted, new_token = _parse_sync_collection(
        resp.body, base_url=calendar_url
    )
    logger.info(
        "  sync-collection: %d changed, %d deleted", len(changed), len(deleted)
    )
    return changed, deleted, new_token


def put_resource(
    client: Client,
    resource_url: str,
    body: bytes,
    *,
    if_none_match: bool = False,
    if_match: str | None = None,
) -> str:
    """PUT an ICS resource. Returns etag (or empty string if absent)."""
    path = urlsplit(resource_url).path or resource_url
    headers: dict[str, str] = {"Content-Type": "text/calendar; charset=utf-8"}
    if if_none_match:
        headers["If-None-Match"] = "*"
    elif if_match is not None:
        headers["If-Match"] = if_match

    try:
        resp = client.request("PUT", path, body=body, headers=headers)
    except HttpStatusError as exc:
        if exc.status == 412:
            raise CalDAVConflictError(
                f"PUT {path}: precondition failed (412)"
            ) from exc
        if exc.status == 404:
            raise CalDAVNotFoundError(f"PUT {path}: not found (404)") from exc
        raise CalDAVError(f"PUT {path}: HTTP {exc.status}") from exc

    etag = resp.headers.get("etag", "")
    return etag.strip().strip('"')


def delete_resource(
    client: Client,
    resource_url: str,
    *,
    if_match: str | None = None,
) -> None:
    """DELETE a resource, optionally conditional on the current etag."""
    path = urlsplit(resource_url).path or resource_url
    headers: dict[str, str] = {}
    if if_match is not None:
        headers["If-Match"] = if_match
    try:
        client.request("DELETE", path, headers=headers)
    except HttpStatusError as exc:
        if exc.status == 412:
            raise CalDAVConflictError(
                f"DELETE {path}: precondition failed (412)"
            ) from exc
        if exc.status == 404:
            raise CalDAVNotFoundError(
                f"DELETE {path}: not found (404)"
            ) from exc
        raise CalDAVError(f"DELETE {path}: HTTP {exc.status}") from exc


def _client_base_url(client: Client, path: str) -> str:
    """Reconstruct a base URL from the client's scheme/netloc and the given path."""
    return f"{client._default_scheme}://{client._default_netloc}{path}"
