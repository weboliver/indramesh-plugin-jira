"""Authenticated, read-only Jira GET adapter for the plugin pull engine (Phase 293).

This is a behavior port of the Core ``app/services/jira_client.py`` READ path into
the plugin package. It is deliberately narrow:

* it never imports an ``app.*`` module (the plugin boundary gate enforces this) —
  the caller supplies the already-validated Source base URL and the PAT resolved
  through ``ctx.credentials()``;
* it classifies ``401``/``403`` as a fatal authentication/authorization failure
  (``JiraPullAuthError``) for JSON and streamed responses alike;
* a ``transport`` can be injected so a test drives the exact HTTP calls without a
  network. ``transport=None`` (the production default) selects the real network.

The IQL page reader keeps the Core client's pagination contract: it validates the
response metadata, never infers ``hasNextPage`` from the locally type-filtered
``values`` size, and returns the entries of the requested Jira object type only.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

# Jira Assets read endpoints this adapter uses. Kept as module constants so the
# selected-schema discovery path cannot accidentally fall back to the list-all
# endpoint: the engine only ever reads the one schema it was asked for.
MYSELF_PATH = "/rest/api/2/myself"
OBJECTSCHEMA_PATH = "/rest/insight/1.0/objectschema/{jira_schema_id}"
OBJECTSCHEMA_LIST_PATH = "/rest/insight/1.0/objectschema/list"
OBJECTSCHEMA_TYPES_PATH = "/rest/insight/1.0/objectschema/{jira_schema_id}/objecttypes"
OBJECTSCHEMA_REFERENCE_TYPES_PATH = (
    "/rest/insight/1.0/objectschema/{jira_schema_id}/referencetypes"
)
OBJECTTYPE_PATH = "/rest/insight/1.0/objecttype/{jira_type_id}"
OBJECTTYPE_ATTRIBUTES_PATH = "/rest/insight/1.0/objecttype/{jira_type_id}/attributes"
OBJECTTYPE_OBJECTS_PATH = "/rest/insight/1.0/objecttype/{jira_type_id}/objects"
OBJECT_PATH = "/rest/insight/1.0/object/{jira_object_id}"
IQL_OBJECTS_PATH = "/rest/insight/1.0/iql/objects"
STATUS_TYPE_PATH = "/rest/assets/1.0/config/statustype"
REFERENCE_TYPE_CREATE_PATH = "/rest/insight/1.0/config/referencetype"
OBJECT_TYPE_CREATE_PATH = "/rest/insight/1.0/objecttype/create"
OBJECT_TYPE_ATTRIBUTE_CREATE_PATH = "/rest/insight/1.0/objecttypeattribute/{jira_type_id}"
OBJECT_CREATE_PATH = "/rest/insight/1.0/object/create"
ICON_GLOBAL_PATH = "/rest/insight/1.0/icon/global"
ATTACHMENTS_PATH = "/rest/insight/1.0/attachments/object/{jira_object_id}"
COMMENTS_PATH = "/rest/assets/1.0/comment/object/{jira_object_id}"

# The only path prefixes an attachment `url` may resolve to. The attachment
# `url` is Jira-controlled input and is NEVER fetched as an absolute URL: only
# its path is used, resolved against the pinned base URL, and it must be an
# attachment-download path (T-293-06-01 — forged host/credential/path rejected).
ATTACHMENT_PATH_PREFIXES = (
    "/rest/insight/1.0/attachments/",
    "/rest/assets/1.0/attachment",
    "/rest/api/2/attachment/content/",
    "/rest/api/3/attachment/content/",
    "/secure/attachment/",
)

# A bounded error body kept in the message; the raw body never enters a task row.
_ERROR_BODY_MAX = 500

# A hard page bound for the authoritative inventory (T-293-05-02): a server that
# never returns a short page must not page forever.
_MAX_INVENTORY_PAGES = 10_000

# Bounded 429 handling (294-04): honor an integer Retry-After capped at 30s for at
# most 3 retries, then surface the 429. A resolve path disables retries (0) so it
# never holds its FOR UPDATE row lock behind a Retry-After sleep.
_MAX_429_RETRIES = 3
_MAX_RETRY_AFTER_SECONDS = 30


def _escape_iql_literal(value: str) -> str:
    """Escape a value embedded in a double-quoted IQL literal (Core parity)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def format_iql_updated_boundary(last_synced_at: datetime) -> str:
    """A Jira IQL minute-precision lower bound that excludes nothing (293-04).

    Jira truncates the "Updated" comparison to the minute, so the bound is
    rounded DOWN to ``:00`` seconds and is inclusive (``>=``): a value updated in
    the same minute as the previous completion is never skipped.
    """
    if not isinstance(last_synced_at, datetime):
        raise ValueError("last_synced_at must be a datetime")
    if last_synced_at.tzinfo is None:
        normalized = last_synced_at.replace(tzinfo=timezone.utc)
    else:
        normalized = last_synced_at.astimezone(timezone.utc)
    return normalized.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")


class JiraPullAuthError(Exception):
    """Fatal: Jira rejected the PAT (401) or forbade the read (403).

    ``status_code`` distinguishes a rejected credential (401 — always fatal) from
    a single forbidden read (403) so an optional read-only sweep can decline just
    that read without ending the whole pull.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class JiraPullApiError(Exception):
    """A bounded, non-auth Jira API failure (status + truncated body)."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"Jira API error {status_code}: {message}")


class JiraPullItemError(Exception):
    """A narrowly recoverable per-item fault (one entry / one blob), never systemic.

    Reserved for a fault confined to a SINGLE Jira entity whose loss cannot be
    allowed to hide behind a successful page: a malformed per-entry payload or a
    failed single media download. The pull records a bounded ``category`` in the
    SyncAudit row, appends the Jira id to the retry list and continues.

    It must never wrap a 401/403 (``JiraPullAuthError``), a malformed page
    envelope, a DB fault or any unknown exception — those stay fatal (T-293-08-01).
    """

    def __init__(self, message: str, *, category: str) -> None:
        self.category = category
        super().__init__(message)


class JiraPullClient:
    """A per-run authenticated Jira reader bound to one validated Source URL.

    One instance is created per pull run and closed on every exit path, so the
    PAT never outlives the run. A ``transport`` is injected by tests only.
    """

    def __init__(
        self,
        base_url: str,
        pat: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
        max_429_retries: int = _MAX_429_RETRIES,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        parts = urlsplit(self.base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise JiraPullApiError(400, "Jira base URL must be an http(s) URL")
        self._max_429_retries = max(0, int(max_429_retries))
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "JiraPullClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        """Shared status handling for the JSON read paths (293-05)."""
        if response.status_code in (401, 403):
            raise JiraPullAuthError(
                "Jira authentication or authorization failed",
                status_code=response.status_code,
            )
        if response.status_code >= 400:
            raise JiraPullApiError(
                response.status_code, response.text[:_ERROR_BODY_MAX]
            )
        if response.status_code == 204:
            return None
        return response.json()

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> int | None:
        """An integer Retry-After delay capped at 30s, or ``None`` if unusable."""
        raw = response.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            seconds = int(str(raw).strip())
        except (TypeError, ValueError):
            return None
        if seconds < 0:
            return None
        return min(seconds, _MAX_RETRY_AFTER_SECONDS)

    async def _send(self, method: str, path: str, *, params=None, json=None) -> Any:
        """One request with bounded 429 handling; 401/403 stay fatal.

        A 429 with a usable integer ``Retry-After`` is retried at most
        ``max_429_retries`` times before the bounded error is raised. Retries are
        disabled (0) on the resolve path so its row lock is never held across a
        sleep.
        """
        attempts = 0
        while True:
            response = await self._client.request(
                method, path, params=params, json=json
            )
            if response.status_code == 429 and attempts < self._max_429_retries:
                delay = self._retry_after_seconds(response)
                if delay is not None:
                    attempts += 1
                    await asyncio.sleep(delay)
                    continue
            return self._decode(response)

    async def _get(self, path: str, *, params: dict | None = None) -> Any:
        """One GET; 401/403 are fatal and never mistaken for an empty result."""
        return await self._send("GET", path, params=params)

    async def _post(self, path: str, *, json: dict | None = None) -> Any:
        """One POST read; the same fatal/empty-vs-error contract as ``_get``."""
        return await self._send("POST", path, json=json)

    async def _put(self, path: str, *, json: dict | None = None) -> Any:
        """One PUT write; the same fatal/empty-vs-error contract as ``_get``."""
        return await self._send("PUT", path, json=json)

    async def update_object(self, jira_object_id: str, payload: dict) -> Any:
        """Replace one Jira object's whole attribute set (294-04, D-01)."""
        return await self._put(
            OBJECT_PATH.format(jira_object_id=jira_object_id), json=payload
        )

    async def create_status_type(self, jira_schema_id: str, payload: dict) -> Any:
        """Create one status type in a Jira Assets schema (294-05, JSYNC-04)."""
        body = dict(payload)
        body.setdefault("objectSchemaId", jira_schema_id)
        return await self._post(STATUS_TYPE_PATH, json=body)

    async def create_reference_type(self, jira_schema_id: str, payload: dict) -> Any:
        """Create one reference type in a Jira Assets schema (294-05)."""
        body = dict(payload)
        body.setdefault("objectSchemaId", jira_schema_id)
        return await self._post(REFERENCE_TYPE_CREATE_PATH, json=body)

    async def get_icons(self) -> Any:
        """The globally available Jira icons (create-object-type fallback)."""
        return await self._get(ICON_GLOBAL_PATH)

    async def create_object_type(self, jira_schema_id: str, payload: dict) -> Any:
        """Create one object type; Assets requires an ``iconId`` (294-05)."""
        body = dict(payload)
        body.setdefault("objectSchemaId", jira_schema_id)
        if "iconId" not in body:
            icons = await self.get_icons()
            if isinstance(icons, list) and icons and isinstance(icons[0], dict):
                body["iconId"] = icons[0].get("id")
        return await self._post(OBJECT_TYPE_CREATE_PATH, json=body)

    async def create_object_type_attribute(
        self, jira_type_id: str, payload: dict
    ) -> Any:
        """Create one attribute on a Jira object type (294-05)."""
        return await self._post(
            OBJECT_TYPE_ATTRIBUTE_CREATE_PATH.format(jira_type_id=jira_type_id),
            json=payload,
        )

    async def create_object(self, payload: dict) -> Any:
        """Create one Jira object from a fully built create payload (294-05)."""
        return await self._post(OBJECT_CREATE_PATH, json=payload)

    async def get_myself(self) -> Any:
        """Read the authenticated Jira user (the connection-test identity probe)."""
        return await self._get(MYSELF_PATH)

    async def get_schema(self, jira_schema_id: str) -> dict:
        """Read exactly the selected Jira schema (never the list-all endpoint)."""
        return await self._get(OBJECTSCHEMA_PATH.format(jira_schema_id=jira_schema_id))

    async def get_schemas(self) -> Any:
        """List the Jira object schemas visible to this PAT (293-07 picker).

        Core parity: Jira wraps the collection in ``objectschemas`` on some
        versions and returns a bare list on others; both shapes are returned
        unchanged for the caller to normalize.
        """
        data = await self._get(OBJECTSCHEMA_LIST_PATH)
        if isinstance(data, dict):
            return data.get("objectschemas", data)
        return data

    @staticmethod
    def _as_list(result: Any, label: str) -> list[dict]:
        """A Jira collection response, or a fatal malformed-envelope error.

        A malformed envelope must never be interpreted as "empty": that would
        make a failed discovery look like a completed one (D-05).
        """
        if result is None:
            return []
        if not isinstance(result, list):
            raise JiraPullApiError(502, f"Jira {label} response is not a list")
        return result

    async def get_object_type(self, jira_type_id: str) -> dict:
        return await self._get(OBJECTTYPE_PATH.format(jira_type_id=jira_type_id))

    async def get_object_types(self, jira_schema_id: str) -> list[dict]:
        """Every ObjectType of exactly the selected Jira schema."""
        result = await self._get(
            OBJECTSCHEMA_TYPES_PATH.format(jira_schema_id=jira_schema_id)
        )
        return self._as_list(result, "object types")

    async def get_object_type_attributes(self, jira_type_id: str) -> list[dict]:
        result = await self._get(
            OBJECTTYPE_ATTRIBUTES_PATH.format(jira_type_id=jira_type_id)
        )
        return self._as_list(result, "object type attributes")

    async def get_object_type_object_ids(
        self, jira_type_id: str, *, page_size: int = 100
    ) -> set[str]:
        """The COMPLETE current Jira object-id inventory for one type (293-05).

        This is the only deletion authority (D-04, T-293-05-02): it fetches the
        unfiltered object-type list — never the incremental IQL result and never
        a locally filtered page — with validated ids and a forward-progress check
        so a stalled or malformed page can never masquerade as an empty type.
        """
        if page_size < 1:
            raise ValueError("page_size must be positive")
        path = OBJECTTYPE_OBJECTS_PATH.format(jira_type_id=jira_type_id)
        object_ids: set[str] = set()
        page = 1
        while True:
            objects = await self._post(
                path, json={"page": page, "resultsPerPage": page_size}
            )
            if objects is None or not isinstance(objects, list):
                raise JiraPullApiError(
                    502, "Jira object-type list response is not a list"
                )
            page_ids: set[str] = set()
            for obj in objects:
                if not isinstance(obj, dict) or obj.get("id") is None:
                    raise JiraPullApiError(
                        502, "Jira object-type list response has an invalid object id"
                    )
                page_ids.add(str(obj["id"]))
            if not objects:
                return object_ids
            if len(objects) == page_size and not page_ids.difference(object_ids):
                raise JiraPullApiError(
                    502, "Jira object-type list pagination did not advance"
                )
            object_ids.update(page_ids)
            if len(objects) < page_size:
                return object_ids
            page += 1
            if page > _MAX_INVENTORY_PAGES:
                raise JiraPullApiError(
                    502, "Jira object-type list pagination exceeded its bound"
                )

    async def get_reference_types(self, jira_schema_id: str) -> list[dict]:
        result = await self._get(
            OBJECTSCHEMA_REFERENCE_TYPES_PATH.format(jira_schema_id=jira_schema_id)
        )
        return self._as_list(result, "reference types")

    async def get_status_types(self, jira_schema_id: str | None = None) -> list[dict]:
        """Status types; ``None`` reads the Jira-global default set.

        The Jira status API wraps its payload inconsistently (bare list, or an
        object with ``values``/``statusTypes``) — all are handled, anything else
        is fatal.
        """
        params = {"objectSchemaId": jira_schema_id} if jira_schema_id else None
        result = await self._get(STATUS_TYPE_PATH, params=params)
        if result is None:
            return []
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("values", "statusTypes"):
                value = result.get(key)
                if isinstance(value, list):
                    return value
        raise JiraPullApiError(502, "Jira status types response is malformed")

    async def get_all_status_types(self) -> list[dict]:
        return await self.get_status_types(None)

    async def get_object(self, jira_object_id: str) -> dict:
        return await self._get(OBJECT_PATH.format(jira_object_id=jira_object_id))

    async def get_object_comments(self, jira_object_id: str) -> list[dict]:
        """The comment list for one Jira Assets object (293-06).

        ``GET /rest/assets/1.0/comment/object/{id}`` (the Assets API, not
        ``/rest/insight/1.0/``). A non-list body is fatal, never "no comments".
        """
        result = await self._get(
            COMMENTS_PATH.format(jira_object_id=jira_object_id)
        )
        return self._as_list(result, "comments")

    async def get_object_attachments(self, jira_object_id: str) -> list[dict]:
        """The attachment list for one Jira Assets object (293-06).

        ``GET /rest/insight/1.0/attachments/object/{id}``. A non-list body is
        fatal, never "no attachments".
        """
        result = await self._get(
            ATTACHMENTS_PATH.format(jira_object_id=jira_object_id)
        )
        return self._as_list(result, "attachments")

    @staticmethod
    def _attachment_path(ref: Any) -> str:
        """Resolve a Jira attachment ``url`` to a pinned-base, allowed path.

        The absolute URL is untrusted (T-293-06-01): its host is discarded and
        only an attachment-download path is accepted. A ref with embedded
        credentials or an unsupported scheme, or a path outside the allow-list,
        is refused before any request is made.
        """
        if not isinstance(ref, str) or not ref.strip():
            raise JiraPullApiError(400, "Jira attachment ref is missing")
        parsed = urlsplit(ref)
        if parsed.scheme and parsed.scheme not in ("http", "https"):
            raise JiraPullApiError(400, "Jira attachment ref has an unsupported scheme")
        if parsed.username or parsed.password:
            raise JiraPullApiError(400, "Jira attachment ref carries credentials")
        path = parsed.path or "/"
        if not path.startswith("/"):
            path = "/" + path
        if not any(path.startswith(prefix) for prefix in ATTACHMENT_PATH_PREFIXES):
            raise JiraPullApiError(
                400, "Jira attachment ref is not an allowed attachment path"
            )
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return path

    async def stream_attachment_blob(self, ref: str):
        """Stream one attachment's raw bytes from the pinned base (293-06).

        The path is validated with :meth:`_attachment_path` FIRST; redirects are
        never followed (httpx default), so an unexpected 3xx is a bounded error
        rather than a request to a foreign host. 401/403 stay fatal for JSON and
        streamed responses alike.
        """
        path = self._attachment_path(ref)
        async with self._client.stream("GET", path) as response:
            if response.status_code in (401, 403):
                raise JiraPullAuthError(
                    "Jira authentication or authorization failed",
                    status_code=response.status_code,
                )
            if response.status_code >= 300:
                raise JiraPullApiError(
                    response.status_code, "Jira attachment download failed"
                )
            async for chunk in response.aiter_bytes():
                yield chunk

    async def get_objects_page(
        self,
        jira_type_id: str,
        *,
        page: int = 1,
        limit: int = 50,
        last_synced_at: datetime | None = None,
    ) -> dict:
        """Read one IQL page for a Jira object type with validated metadata.

        Mirrors the Core client: the IQL type name is resolved from the object
        type, response pagination is validated before any entry is trusted, and
        only entries whose own ``objectType.id`` matches are returned. When
        ``last_synced_at`` is given, the query is bounded to objects updated at
        or after its minute-rounded value (the incremental window, 293-04).
        """
        if page < 1 or limit < 1:
            raise ValueError("page and limit must both be positive")

        object_type = await self.get_object_type(jira_type_id)
        object_type_name = (
            object_type.get("name") if isinstance(object_type, dict) else None
        )
        if not isinstance(object_type_name, str) or not object_type_name:
            raise JiraPullApiError(
                502, f"Jira object type {jira_type_id} has no usable name"
            )

        iql_query = f'objectType = "{_escape_iql_literal(object_type_name)}"'
        if last_synced_at is not None:
            boundary = format_iql_updated_boundary(last_synced_at)
            iql_query += f' AND "Updated" >= "{_escape_iql_literal(boundary)}"'
        result = await self._get(
            IQL_OBJECTS_PATH,
            params={"qlQuery": iql_query, "page": page, "resultPerPage": limit},
        )
        if not isinstance(result, dict):
            raise JiraPullApiError(502, "Jira IQL response is not an object")

        try:
            page_number = int(result["pageNumber"])
            start_index = int(result["startIndex"])
            to_index = int(result["toIndex"])
            total = int(result["totalFilterCount"])
        except (KeyError, TypeError, ValueError):
            raise JiraPullApiError(
                502, "Jira IQL response has invalid pagination metadata"
            ) from None
        if (
            page_number != page
            or total < 0
            or start_index < 1
            or to_index < start_index - 1
        ):
            raise JiraPullApiError(
                502, "Jira IQL response has non-advancing pagination metadata"
            )
        if to_index > total:
            raise JiraPullApiError(
                502, "Jira IQL response pagination exceeds its result total"
            )

        entries = result.get("objectEntries", [])
        if not isinstance(entries, list):
            raise JiraPullApiError(502, "Jira IQL response has invalid object entries")
        values = [
            entry
            for entry in entries
            if isinstance(entry, dict)
            and str((entry.get("objectType") or {}).get("id")) == str(jira_type_id)
        ]
        response_page_size = int(result.get("pageSize", limit))
        has_next_page = to_index < total
        total_pages = (total + max(response_page_size, 1) - 1) // max(
            response_page_size, 1
        )
        return {
            "values": values,
            "page": page_number,
            "nextPage": page_number + 1 if has_next_page else None,
            "hasNextPage": has_next_page,
            "total": total,
            "totalPages": total_pages,
        }
