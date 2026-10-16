# API Reference

URL Shortener API reference. This document describes the behaviour in `app/api/*`,
`app/schemas/*`, `app/services/*` and `app/core/errors.py`. FastAPI also serves a
machine-readable schema at `GET /openapi.json`, plus interactive documentation at
`GET /docs` (Swagger UI) and `GET /redoc`.

- **Base URL:** wherever the service is deployed, e.g. `http://localhost:8000`. The
  `short_url` field is built from the `BASE_URL` setting, not from the request host.
- **Content type:** request and response bodies are JSON (`application/json`), apart
  from the empty `204`, `307` and `HEAD` responses.
- **Authentication:** every `/api/links*` endpoint requires the `X-API-Key` header.
  Redirects and probes are public. See [Authentication](#authentication).
- **Timestamps:** responses use ISO 8601 in UTC with a `Z` suffix, e.g.
  `2025-01-15T10:30:00.123456Z`. Fractional seconds appear only when they are non-zero.
  Request timestamps (`expires_at`, `start`, `end`) accept ISO 8601 with a `Z`, a
  numeric offset (`+01:00`, `-05:00`, `+05:30`) or no offset. Offsets are converted to
  UTC, and **values without an offset are treated as UTC**.
- **Short codes:** generated codes are base62 (`[A-Za-z0-9]`) and 7 characters long
  by default (`SHORT_CODE_LENGTH`). Codes are case-sensitive. Any `{code}` path value
  that doesn't match `^[A-Za-z0-9]{1,32}$` gets `404 Link not found` without a
  database lookup.

## Contents

- [Authentication](#authentication)
- [Error format](#error-format)
- [POST /api/links](#post-apilinks): create a short link
- [GET /api/links/{code}](#get-apilinkscode): link metadata
- [PATCH /api/links/{code}](#patch-apilinkscode): set, change or clear the expiry
- [DELETE /api/links/{code}](#delete-apilinkscode): deactivate a link
- [GET /api/links/{code}/stats](#get-apilinkscodestats): daily click stats
- [GET /api/links/{code}/stats/hourly](#get-apilinkscodestatshourly): clicks per UTC hour
- [GET /{code}](#get-code): redirect
- [HEAD /{code}](#head-code): redirect check without recording a click
- [GET /healthz and HEAD /healthz](#get-healthz-and-head-healthz): liveness probe
- [GET /readyz and HEAD /readyz](#get-readyz-and-head-readyz): readiness probe
- [Schemas](#schemas)

---

## Authentication

All endpoints under `/api/links` (create, lookup, PATCH expiry, deactivate, daily and
hourly stats) require a shared API key:

```
X-API-Key: <the value of the API_KEY setting>
```

- The key is compared in constant time.
- A missing, empty or wrong key returns `401 Unauthorized` with the header
  `WWW-Authenticate: ApiKey` and the body:

  ```json
  {"detail": "Invalid or missing API key"}
  ```

- **Fail closed:** if the server has no `API_KEY` configured (unset, empty or
  whitespace-only), every `/api/links*` request returns this 401. Nothing is created,
  read or changed.
- `API_KEY` must be at least **16 characters**. A shorter value stops the server at
  startup with a validation error.
- Authentication is checked before the request body fields, path and query
  parameters are validated and before any database lookup, so a request without a
  valid key gets `401` even when its body fails validation, its query parameters are
  invalid or the code is unknown. One exception: a body that is not syntactically
  valid JSON is rejected with `422` (`json_invalid`) before the key is checked.
  Nothing is read or changed in either case.
- Public endpoints that need no key: `GET`/`HEAD /{code}`, `GET`/`HEAD /healthz`,
  `GET`/`HEAD /readyz`, `/docs`, `/redoc` and `/openapi.json`.
- In `/openapi.json` the scheme is published as `ApiKeyAuth`
  (`{"type": "apiKey", "in": "header", "name": "X-API-Key"}`). In Swagger UI, use
  **Authorize** to enter the key.

The key is never logged.

---

## Error format

Every error response is JSON with a single `detail` key:

```json
{"detail": "Link not found"}
```

| Status | When | `detail` |
|--------|------|----------|
| 400 | Invalid `Idempotency-Key` header | `"Invalid Idempotency-Key header"` |
| 401 | Missing, empty or wrong `X-API-Key` on `/api/links*`, or no `API_KEY` configured on the server. The response includes `WWW-Authenticate: ApiKey`. | `"Invalid or missing API key"` |
| 404 | Unknown or malformed short code | `"Link not found"` |
| 404 | No route matches the path (e.g. `/api/links/abc/unknown`) | `"Not Found"` |
| 405 | The route exists but not for this method (e.g. `PUT /api/links/abc`, `GET /api/links`, `POST /healthz`). The response includes an `Allow` header. | `"Method Not Allowed"` |
| 409 | `Idempotency-Key` reused with a different payload (different `url`, or `expires_at` added, changed or removed), or a concurrent-request conflict | `"Idempotency-Key already used with a different payload"` / `"Idempotency-Key conflict, please retry"` |
| 410 | Redirect to a deactivated link (takes precedence over expiry) | `"Link has been deactivated"` |
| 410 | Redirect to an expired link (`now >= expires_at`) | `"Link has expired"` |
| 422 | Request validation failed (body, URL, `expires_at` format, query parameter, unknown PATCH field) | A list of Pydantic error objects (see below) |
| 422 | Domain validation failed: `expires_at` not in the future, or an invalid hourly stats range | A string, e.g. `"expires_at must be in the future"` (see the endpoint sections) |
| 500 | Unexpected server error | Always exactly `"Internal Server Error"` |
| 503 | Short-code allocation exhausted | `"Could not allocate a unique short code, please retry"` |
| 503 | Database error on any endpoint (including a locked SQLite database after the busy timeout, or no free pool connection after `DB_POOL_TIMEOUT_SECONDS`) | `"Service temporarily unavailable"` |
| 503 | Readiness check failed | `"Database is not ready"` |

Error bodies never contain stack traces, exception class names, exception messages,
SQL or file paths. Server-side failures are logged in full; see
[OPERATIONS.md](OPERATIONS.md).

### 422 validation errors

A `422` has one of two `detail` shapes:

1. **Request validation** (FastAPI/Pydantic): `detail` is a list of error objects.
   Each item has `type`, `loc`, `msg` and `input`, and some also have `ctx`. Example
   for a rejected URL:

   ```json
   {
     "detail": [
       {
         "type": "value_error",
         "loc": ["body", "url"],
         "msg": "Value error, URL scheme must be http or https",
         "input": "javascript:alert(1)",
         "ctx": {"error": {}}
       }
     ]
   }
   ```

2. **Domain validation** (rules that depend on the server clock or on several
   parameters): `detail` is a string, for example
   `{"detail": "expires_at must be in the future"}` or
   `{"detail": "range must not exceed 744 hours"}`.

Clients should rely only on the status code and the presence of `detail`. The exact
fields of each Pydantic item come from the library and may change between versions.

---

## POST /api/links

Creates a new active short link, optionally with an expiry.

### Request

Headers:

| Header | Required | Description |
|--------|----------|-------------|
| `X-API-Key` | yes | See [Authentication](#authentication). |
| `Content-Type: application/json` | yes | |
| `Idempotency-Key` | no | Makes the request idempotent. 1–255 characters and not blank (whitespace-only values are rejected). Keys are global (not tied to a client) and never expire in this release. |

Body (`LinkCreate`):

```json
{"url": "https://example.com/a?b=1"}
```

```json
{"url": "https://example.com/campaign", "expires_at": "2025-12-31T23:59:00Z"}
```

| Field | Type | Rules |
|-------|------|-------|
| `url` | string (strict: numbers, booleans etc. are not coerced) | Required. See [URL validation rules](#url-validation-rules). Stored and returned **exactly as submitted** (no normalization). |
| `expires_at` | ISO 8601 date-time string or `null` | Optional; omitted or `null` means the link never expires. See [Expiry rules](#expiry-rules). |

Unknown extra fields in the body are ignored.

#### URL validation rules

A URL is accepted only if **all** of the following hold:

1. Length is 1–2048 characters. A 2048-character URL is accepted; a 2049-character
   URL is rejected.
2. It contains no whitespace, no ASCII control characters (`0x00`–`0x20`, `0x7F`) and
   no non-ASCII characters. Send internationalised domain names as punycode and other
   non-ASCII characters percent-encoded.
3. It can be parsed by `urllib.parse.urlsplit`, and any port is numeric and in range.
4. The scheme is `http` or `https`, case-insensitive (`HTTPS://example.com` is accepted
   and stored unchanged).
5. The scheme is followed by `://`.
6. The host is not empty.

| Example | Result |
|---------|--------|
| `https://example.com/a?b=1` | 201 |
| `HTTPS://example.com` | 201 |
| `http://localhost:8080/x` | 201 |
| `""` (empty) | 422 `URL must not be empty` |
| 2049+ characters | 422 `URL must be at most 2048 characters` |
| `http://exa mple.com`, a URL containing `\n` or `\t`, `https://bücher.de` | 422 `URL must not contain whitespace, control or non-ASCII characters` |
| `http://example.com:99999` | 422 `invalid URL` |
| `javascript:alert(1)`, `data:text/html,hi`, `file:///etc/passwd`, `ftp://example.com`, `mailto:a@b.c` | 422 `URL scheme must be http or https` |
| `/path`, `example.com` | 422 (no `http`/`https` scheme) |
| `http://`, `https:///x` | 422 `URL must include a host` |

When validation fails, nothing is stored.

#### Expiry rules

These rules apply to `expires_at` in `POST /api/links` and `PATCH /api/links/{code}`:

- The value must be an ISO 8601 date-time **string** or `null`. Numbers and booleans
  are rejected with a Pydantic 422 (`expires_at must be an ISO 8601 datetime string or
  null`); they are never interpreted as Unix timestamps.
- A value with an offset is converted to UTC (`2026-01-01T00:59:00+01:00` is stored as
  `2025-12-31T23:59:00`). A value **without an offset is treated as UTC**. It is stored
  as naive UTC. A value whose UTC conversion leaves the supported date range (e.g.
  `9999-12-31T23:00:00-05:00`) is rejected with a Pydantic 422
  (`datetime is out of range`).
- The value must be **strictly after the server's current UTC time**. Otherwise the
  response is `422 {"detail": "expires_at must be in the future"}` and nothing is
  stored. (On `POST`, this check is skipped for an idempotent replay; see below.)
- Responses return the value in UTC with a `Z` suffix, e.g. `"2025-12-31T23:59:00Z"`,
  or `null`.
- The link stops redirecting **at** that instant: from `now >= expires_at` onwards,
  `GET`/`HEAD /{code}` return `410 {"detail": "Link has expired"}`. The rule is
  evaluated on every request (no caching), so changes take effect immediately.
- An expired link is not deactivated: `is_active` stays `true`, metadata and stats
  remain available, and you can extend or clear the expiry with
  [PATCH](#patch-apilinkscode).

### Responses

**201 Created**: `LinkCreatedResponse`

Headers:

- `Location: /api/links/{code}` (a relative path to the metadata resource)
- `Idempotent-Replayed: true` only when the response replays an earlier request with
  the same `Idempotency-Key`. It is absent on the first response.

```json
{
  "code": "aB3xY9Q",
  "short_url": "http://localhost:8000/aB3xY9Q",
  "url": "https://example.com/campaign",
  "is_active": true,
  "created_at": "2025-01-15T10:30:00.123456Z",
  "expires_at": "2025-12-31T23:59:00Z"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `code` | string | The short code. |
| `short_url` | string | `BASE_URL` with trailing `/` stripped, then `/`, then `code`. |
| `url` | string | The original URL, unchanged. |
| `is_active` | boolean | `true` for a new link. On a replay, this reflects the link's current state. |
| `created_at` | string (date-time, UTC) | Creation time. |
| `expires_at` | string (date-time, UTC) or `null` | Expiry instant, or `null` if the link never expires. On a replay, this reflects the link's current value. |

| Status | Body | When |
|--------|------|------|
| 400 | `{"detail": "Invalid Idempotency-Key header"}` | `Idempotency-Key` is empty, whitespace-only or longer than 255 characters. |
| 401 | `{"detail": "Invalid or missing API key"}` | See [Authentication](#authentication). |
| 409 | `{"detail": "Idempotency-Key already used with a different payload"}` | The key was already used with a different `url`, or with a different `expires_at` (added, changed or removed). No link is created. |
| 409 | `{"detail": "Idempotency-Key conflict, please retry"}` | Rare: a concurrent request with the same key could not be resolved. Retrying is safe. |
| 422 | `{"detail": [...]}` | Missing or invalid JSON body, missing `url`, non-string `url`, a URL that breaks the validation rules, or an `expires_at` that is not a date-time string or is out of range. |
| 422 | `{"detail": "expires_at must be in the future"}` | `expires_at` is at or before the server's current time (new requests only). |
| 503 | `{"detail": "Could not allocate a unique short code, please retry"}` | All `CODE_MAX_ATTEMPTS` (default 5) generated codes collided or were reserved. This is extremely unlikely in practice. |
| 503 | `{"detail": "Service temporarily unavailable"}` | Database error. |

### Idempotency semantics

| Situation | Result |
|-----------|--------|
| No `Idempotency-Key` | A new link is created on every request. Identical URLs are **not** deduplicated. |
| New key | A link is created and the key is stored in the same transaction. |
| Same key, same `url`, same `expires_at` (or both without `expires_at`) | `201` with the original link (same `code`) plus `Idempotent-Replayed: true`. No new row is created. |
| Same key, same instant written with a different offset (`2025-12-31T23:59:00Z` vs `2026-01-01T00:59:00+01:00`) | Replay: the instant is compared after UTC normalisation. |
| Same key, different `url` | `409`. Nothing is created. |
| Same key, `expires_at` added, changed or removed | `409`. Nothing is created. |
| Same key, replayed after the stored `expires_at` has passed | Replay (`201`, `Idempotent-Replayed: true`) with the stored link. The "must be in the future" check is skipped on replay, just as a deactivated link still replays. |
| Keys stored before expiry support existed | Still replay when the request has no `expires_at` (`null` or omitted). |
| Different keys, same `url` | Separate links are created. |
| Concurrent requests with the same key | A database unique constraint guarantees that at most one link is created. The other request gets a replay or a `409`, never a `500`. |

Keys are compared byte-for-byte (case-sensitive). The payload is compared by a SHA-256
hash: of the `url` string alone when `expires_at` is absent or `null`, otherwise of the
`url` plus the canonical UTC instant of `expires_at`. The replay/conflict check runs
before the "must be in the future" check.

### Examples

```bash
curl -i -X POST http://localhost:8000/api/links \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/a?b=1"}'
```

```
HTTP/1.1 201 Created
content-type: application/json
location: /api/links/aB3xY9Q

{"code":"aB3xY9Q","short_url":"http://localhost:8000/aB3xY9Q","url":"https://example.com/a?b=1","is_active":true,"created_at":"2025-01-15T10:30:00.123456Z","expires_at":null}
```

With an expiry given in local time (converted to UTC):

```bash
curl -s -X POST http://localhost:8000/api/links \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/campaign", "expires_at": "2026-01-01T00:59:00+01:00"}'
# {"code":"Kx7pQ2m",...,"expires_at":"2025-12-31T23:59:00Z"}
```

Idempotent replay (the second identical request):

```bash
curl -i -X POST http://localhost:8000/api/links \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-42' \
  -d '{"url": "https://example.com/a?b=1"}'
```

```
HTTP/1.1 201 Created
location: /api/links/Zk81mPq
idempotent-replayed: true

{"code":"Zk81mPq", ...same body as the first response...}
```

Invalid URL and past expiry:

```bash
curl -s -X POST http://localhost:8000/api/links -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' -d '{"url": "ftp://example.com"}'
# 422 {"detail":[{"type":"value_error","loc":["body","url"],"msg":"Value error, URL scheme must be http or https",...}]}

curl -s -X POST http://localhost:8000/api/links -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/", "expires_at": "2020-01-01T00:00:00Z"}'
# 422 {"detail":"expires_at must be in the future"}
```

---

## GET /api/links/{code}

Returns metadata for a link, whether it is active, deactivated or expired.

### Request

| Parameter | In | Description |
|-----------|----|-------------|
| `X-API-Key` | header | Required. |
| `code` | path | Short code. |

### Responses

**200 OK**: `LinkDetailResponse`

```json
{
  "code": "aB3xY9Q",
  "short_url": "http://localhost:8000/aB3xY9Q",
  "url": "https://example.com/a?b=1",
  "is_active": true,
  "created_at": "2025-01-15T10:30:00.123456Z",
  "expires_at": "2025-12-31T23:59:00Z",
  "deactivated_at": null,
  "is_expired": false,
  "total_clicks": 42
}
```

This response has all the fields of `LinkCreatedResponse` (including `expires_at`),
plus:

| Field | Type | Description |
|-------|------|-------------|
| `deactivated_at` | string (date-time, UTC) or `null` | When the link was deactivated. `null` while it is active. |
| `is_expired` | boolean | `true` if and only if `expires_at` is not `null` and the server's current time is at or after `expires_at`. Independent of `is_active`. |
| `total_clicks` | integer | All-time number of recorded clicks. |

| Status | Body | When |
|--------|------|------|
| 401 | `{"detail": "Invalid or missing API key"}` | See [Authentication](#authentication). |
| 404 | `{"detail": "Link not found"}` | Unknown code, or a code containing characters outside `[A-Za-z0-9]` or longer than 32 characters. |
| 503 | `{"detail": "Service temporarily unavailable"}` | Database error. |

### Example

```bash
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/api/links/aB3xY9Q
```

---

## PATCH /api/links/{code}

Sets, changes or clears the expiry of an existing link.

### Request

| Parameter | In | Description |
|-----------|----|-------------|
| `X-API-Key` | header | Required. |
| `Content-Type: application/json` | header | Required. |
| `code` | path | Short code. |

Body (`LinkExpiryUpdate`). The `expires_at` field is **required** but may be `null`;
any other field is rejected:

```json
{"expires_at": "2025-12-31T23:59:00Z"}
```

```json
{"expires_at": null}
```

| Field | Type | Rules |
|-------|------|-------|
| `expires_at` | ISO 8601 date-time string or `null` | A non-null value follows the [Expiry rules](#expiry-rules): normalised to UTC (naive values treated as UTC) and strictly after the current time. `null` clears the expiry, so the link never expires. |

Behaviour:

- Only `expires_at` changes. `url`, `code`, `is_active`, `created_at`,
  `deactivated_at` and idempotency records are untouched.
- PATCH **never reactivates** a link. A deactivated link stays deactivated
  (`is_active: false`) and keeps returning `410 Link has been deactivated`, whatever
  its expiry.
- Extending or clearing the expiry of an expired (but not deactivated) link makes it
  redirect again immediately.
- The change takes effect on the next request; nothing is cached.

### Responses

**200 OK**: the updated `LinkDetailResponse` (same shape as
[GET /api/links/{code}](#get-apilinkscode)).

| Status | Body | When |
|--------|------|------|
| 200 | `LinkDetailResponse` | Expiry set, changed or cleared. |
| 401 | `{"detail": "Invalid or missing API key"}` | See [Authentication](#authentication). |
| 404 | `{"detail": "Link not found"}` | Unknown or malformed code. |
| 422 | `{"detail": [...]}` | Missing or invalid JSON body, missing `expires_at`, unknown extra fields (e.g. `url`, `is_active`), or a value that is not a date-time string or `null`, or is out of range. |
| 422 | `{"detail": "expires_at must be in the future"}` | The value is at or before the current time. The stored value is unchanged. |
| 503 | `{"detail": "Service temporarily unavailable"}` | Database error. |

Order of checks: syntactically invalid JSON (422 list), authentication (401), body
validation (422 list), link lookup (404), then the future-instant check (422 string).

### Examples

```bash
# Set or change the expiry
curl -s -X PATCH http://localhost:8000/api/links/aB3xY9Q \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"expires_at": "2025-12-31T23:59:00Z"}'
# {"code":"aB3xY9Q",...,"expires_at":"2025-12-31T23:59:00Z","deactivated_at":null,"is_expired":false,"total_clicks":42}

# Clear it
curl -s -X PATCH http://localhost:8000/api/links/aB3xY9Q \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"expires_at": null}'
# {"code":"aB3xY9Q",...,"expires_at":null,"deactivated_at":null,"is_expired":false,"total_clicks":42}
```

---

## DELETE /api/links/{code}

Deactivates (soft-deletes) a link. The row and its click history are kept, metadata
and stats stay available, and redirects return `410`. Calling it again has no further
effect. Requires `X-API-Key`.

### Responses

| Status | Body | When |
|--------|------|------|
| 204 | *(empty)* | The link is now inactive. On the first call, `is_active` becomes `false` and `deactivated_at` is set to the current UTC time. Later calls also return `204` and leave `deactivated_at` unchanged. |
| 401 | `{"detail": "Invalid or missing API key"}` | See [Authentication](#authentication). |
| 404 | `{"detail": "Link not found"}` | Unknown or malformed code. |
| 503 | `{"detail": "Service temporarily unavailable"}` | Database error. |

You can't reactivate a link or delete it permanently in this release.

### Example

```bash
curl -i -X DELETE -H "X-API-Key: $API_KEY" http://localhost:8000/api/links/aB3xY9Q
# HTTP/1.1 204 No Content
```

---

## GET /api/links/{code}/stats

Returns aggregated daily click statistics. Works for active, deactivated and expired
links. Requires `X-API-Key`. Counts are aggregated in the database (`GROUP BY` UTC
day); the response contract is unchanged from earlier releases.

### Request

| Parameter | In | Type | Default | Description |
|-----------|----|------|---------|-------------|
| `code` | path | string | n/a | Short code. |
| `days` | query | integer, 1–365 | `30` | Window for `clicks_per_day`: the last `days` UTC calendar days, **including today**. `days=1` means today only. |

### Responses

**200 OK**: `LinkStatsResponse`

```json
{
  "code": "aB3xY9Q",
  "total_clicks": 5,
  "clicks_per_day": [
    {"date": "2025-01-14", "count": 2},
    {"date": "2025-01-15", "count": 3}
  ],
  "top_referrers": [
    {"referrer": "https://news.example.org/", "count": 3},
    {"referrer": null, "count": 2}
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `code` | string | The short code. |
| `total_clicks` | integer | **All-time** click count. The `days` window does not affect it. |
| `clicks_per_day` | array of `{date: "YYYY-MM-DD", count: int}` | Clicks grouped by UTC calendar date within the window, sorted by date ascending. Days without clicks are **omitted**. |
| `top_referrers` | array of `{referrer: string \| null, count: int}` | **All-time** referrer counts, up to 10 entries, sorted by `count` descending and then by `referrer` ascending. On a tie, `null` sorts after non-null values. Clicks without a `Referer` header (or with an empty one) are grouped under `referrer: null`. |

A link with no clicks returns `total_clicks: 0`, `clicks_per_day: []` and
`top_referrers: []`.

| Status | Body | When |
|--------|------|------|
| 401 | `{"detail": "Invalid or missing API key"}` | See [Authentication](#authentication). |
| 404 | `{"detail": "Link not found"}` | Unknown or malformed code. |
| 422 | `{"detail": [...]}` | `days` is not an integer or is outside 1–365 (e.g. `days=0`, `days=366`). |
| 503 | `{"detail": "Service temporarily unavailable"}` | Database error. |

### Example

```bash
curl -s -H "X-API-Key: $API_KEY" 'http://localhost:8000/api/links/aB3xY9Q/stats?days=7'
```

---

## GET /api/links/{code}/stats/hourly

Returns the number of clicks per **UTC hour** for one link over the half-open range
`[start, end)`, with every hour listed (hours without clicks have `count: 0`). Works
for active, deactivated and expired links. Requires `X-API-Key`. Counts are aggregated
in the database (`GROUP BY` UTC hour).

### Request

| Parameter | In | Type | Default | Description |
|-----------|----|------|---------|-------------|
| `code` | path | string | n/a | Short code. |
| `start` | query | ISO 8601 date-time | see below | Inclusive range start. Floored to the top of its UTC hour. |
| `end` | query | ISO 8601 date-time | see below | Exclusive range end. Rounded **up** to the next top of a UTC hour (unchanged if it is already on the hour). |

Range rules:

- **Both or neither:** supply both `start` and `end`, or neither. Supplying only one
  returns `422 {"detail": "start and end must be supplied together"}`.
- **Default (both omitted):** the last 24 full UTC hours plus the current hour, i.e.
  `start` = top of the current UTC hour − 24 h and `end` = top of the current UTC
  hour + 1 h, giving **25 buckets**. The last bucket is the current, still-running
  hour.
- **Time zones:** values with an offset are converted to UTC; values without an
  offset are treated as UTC. Buckets are always UTC hours.
- **Alignment:** `start` is floored and `end` is rounded up to whole UTC hours, so
  `start=2025-11-20T19:15:00Z&end=2025-11-20T19:45:00Z` returns the single bucket
  `19:00Z`. The aligned values are returned in the response.
- **Half-open:** a click at exactly `end` is not counted; a click at exactly `start`
  is.
- After alignment, `start` must be before `end`, otherwise
  `422 {"detail": "start must be before end"}`.
- **Maximum range: 744 hours** (31 days) after alignment. A longer range returns
  `422 {"detail": "range must not exceed 744 hours"}`.
- The range is validated before the link is looked up, so an invalid range returns
  `422` even for an unknown code.

> **URL-encode `+` as `%2B`.** In a query string, a literal `+` is decoded as a space,
> so `start=2025-11-20T21:00:00+05:30` arrives as `2025-11-20T21:00:00 05:30` and is
> rejected with 422. Send `start=2025-11-20T21:00:00%2B05:30`, use `Z` (UTC), or let
> your client encode the value (e.g. `curl -G --data-urlencode`). Negative offsets
> such as `-05:00` need no encoding.

### Responses

**200 OK**: `LinkHourlyStatsResponse`

Request:

```bash
curl -s -H "X-API-Key: $API_KEY" \
  'http://localhost:8000/api/links/aB3xY9Q/stats/hourly?start=2025-11-20T18:30:00Z&end=2025-11-20T22:00:00Z'
```

Response (`start` floored from `18:30` to `18:00`):

```json
{
  "code": "aB3xY9Q",
  "start": "2025-11-20T18:00:00Z",
  "end": "2025-11-20T22:00:00Z",
  "total_clicks": 1043,
  "clicks_per_hour": [
    {"hour": "2025-11-20T18:00:00Z", "count": 12},
    {"hour": "2025-11-20T19:00:00Z", "count": 871},
    {"hour": "2025-11-20T20:00:00Z", "count": 160},
    {"hour": "2025-11-20T21:00:00Z", "count": 0}
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `code` | string | The short code. |
| `start` | string (date-time, UTC) | Aligned inclusive start (top of an hour). |
| `end` | string (date-time, UTC) | Aligned exclusive end (top of an hour). |
| `total_clicks` | integer | Sum of the counts **within the range** (unlike the daily stats, where `total_clicks` is all-time). |
| `clicks_per_hour` | array of `{hour, count}` | One entry for every hour in `[start, end)`, ascending. `hour` is the top of the UTC hour with a `Z` suffix; hours without clicks have `count: 0`. |

| Status | Body | When |
|--------|------|------|
| 401 | `{"detail": "Invalid or missing API key"}` | See [Authentication](#authentication). |
| 404 | `{"detail": "Link not found"}` | Unknown or malformed code (only checked once the range is valid). |
| 422 | `{"detail": [...]}` | `start` or `end` is not a parseable ISO 8601 date-time (including a `+` that was not encoded). |
| 422 | `{"detail": "start and end must be supplied together"}` | Only one of `start`/`end` was given. |
| 422 | `{"detail": "start must be before end"}` | The aligned `start` is not before the aligned `end`. |
| 422 | `{"detail": "range must not exceed 744 hours"}` | The aligned range is longer than 744 hours. |
| 422 | `{"detail": "datetime out of range"}` | Converting or aligning a value leaves the supported date range (e.g. an `end` in the last hour of year 9999). |
| 503 | `{"detail": "Service temporarily unavailable"}` | Database error. |

### Examples

```bash
# Default: last 24 hours plus the current hour (25 buckets)
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/api/links/aB3xY9Q/stats/hourly

# A range given in IST (UTC+05:30), with '+' encoded as %2B
curl -s -H "X-API-Key: $API_KEY" \
  'http://localhost:8000/api/links/aB3xY9Q/stats/hourly?start=2025-11-20T21:00:00%2B05:30&end=2025-11-20T23:00:00%2B05:30'
# start 15:30Z is floored to 15:00Z; end 17:30Z is rounded up to 18:00Z (3 buckets)

# Let curl do the encoding
curl -s -G -H "X-API-Key: $API_KEY" http://localhost:8000/api/links/aB3xY9Q/stats/hourly \
  --data-urlencode 'start=2025-11-20T20:00:00+01:00' \
  --data-urlencode 'end=2025-11-20T23:00:00+01:00'
```

---

## GET /{code}

Public redirect: redirects to the original URL and records a click event. No API key
is needed.

### Request

| Parameter | In | Description |
|-----------|----|-------------|
| `code` | path | Short code. |
| `Referer` | header (optional) | Stored on the click event, truncated to 2048 characters. A missing or empty header is stored as `null`. |
| `User-Agent` | header (optional) | Stored on the click event, truncated to 512 characters. A missing or empty header is stored as `null`. |

Oversized `Referer`/`User-Agent` values are truncated and never cause an error. The
client IP is **never** stored. If `IP_HASH_SALT` is configured, the service stores
an HMAC-SHA256 hash of the client address as reported by the ASGI server. The app
itself ignores `X-Forwarded-For`.

Order of checks:

1. Malformed code (not `^[A-Za-z0-9]{1,32}$`) or unknown code: `404`.
2. Deactivated link: `410 {"detail": "Link has been deactivated"}`. Deactivation
   **takes precedence** over expiry.
3. Expired link (server time `>= expires_at`): `410 {"detail": "Link has expired"}`.
4. Otherwise: `307` and one click is recorded.

### Responses

| Status | Headers / Body | When |
|--------|----------------|------|
| 307 Temporary Redirect | `Location: <original URL, byte-for-byte as stored>`, `Cache-Control: no-store`. Empty body. | The link is active and not expired. One click event is recorded (best-effort). |
| 404 | `{"detail": "Link not found"}`, `Cache-Control: no-store`, no `Location`. | Unknown or malformed code. No click is recorded. |
| 410 Gone | `{"detail": "Link has been deactivated"}`, `Cache-Control: no-store`, no `Location`. | The link has been deactivated (whether or not it has also expired). No click is recorded. |
| 410 Gone | `{"detail": "Link has expired"}`, `Cache-Control: no-store`, no `Location`. | The link is active but `expires_at` has been reached. No click is recorded. |
| 503 | `{"detail": "Service temporarily unavailable"}` | The link lookup itself failed (database unavailable). No internal details are returned. |

`404` and `410` responses on this path carry `Cache-Control: no-store`, so browsers
and CDNs never cache them. A link that is created, extended or cleared afterwards
works immediately.

Click recording is best-effort. If writing the click event fails (for example because
SQLite stays locked for longer than `SQLITE_BUSY_TIMEOUT_MS`), one WARNING is logged
and the client still gets the `307`.

Fixed routes (`/healthz`, `/readyz`, `/docs`, `/redoc`, `/openapi.json`, `/api/...`)
are registered before this catch-all route, so it never shadows them. Generated codes
never match a reserved word.

### Example

```bash
curl -i http://localhost:8000/aB3xY9Q
```

```
HTTP/1.1 307 Temporary Redirect
location: https://example.com/a?b=1
cache-control: no-store
content-length: 0
```

```bash
curl -i http://localhost:8000/Kx7pQ2m     # expired
```

```
HTTP/1.1 410 Gone
cache-control: no-store
content-type: application/json

{"detail":"Link has expired"}
```

---

## HEAD /{code}

Returns the same status and `Location` / `Cache-Control` headers as `GET /{code}`
(`307`, `404`, `410` for deactivated or expired, or `503`) with no body. It does
**not** record a click. Public. This endpoint is not listed in the OpenAPI schema.

```bash
curl -I http://localhost:8000/aB3xY9Q
```

---

## GET /healthz and HEAD /healthz

Liveness probe. It never touches the database. Public.

| Method | Status | Body |
|--------|--------|------|
| `GET` | 200 | `{"status": "ok"}` (`HealthResponse`) |
| `HEAD` | 200 | *(empty)* |

`HEAD /healthz` is not listed in the OpenAPI schema. Other methods (e.g. `POST`)
return `405`.

```bash
curl -s http://localhost:8000/healthz     # {"status":"ok"}
curl -I http://localhost:8000/healthz     # HTTP/1.1 200 OK
```

---

## GET /readyz and HEAD /readyz

Readiness probe. It runs `SELECT 1` on a database session. Public.

| Method | Status | Body | When |
|--------|--------|------|------|
| `GET` | 200 | `{"status": "ready"}` | The database query succeeded. |
| `HEAD` | 200 | *(empty)* | The database query succeeded. |
| `GET`, `HEAD` | 503 | `{"detail": "Database is not ready"}` (empty for `HEAD`) | The query failed. |
| `GET`, `HEAD` | 503 | `{"detail": "Service temporarily unavailable"}` (empty for `HEAD`) | A database error was raised before the query could run, e.g. while opening the session. |

The body never contains exception text, SQL or a stack trace. `HEAD /readyz` is not
listed in the OpenAPI schema. Other methods (e.g. `POST`) return `405`.

```bash
curl -s http://localhost:8000/readyz      # {"status":"ready"}
curl -I http://localhost:8000/readyz      # HTTP/1.1 200 OK
```

---

## Schemas

These named schemas appear in `/openapi.json`:

| Schema | Used by |
|--------|---------|
| `LinkCreate` (`url`, optional `expires_at`) | `POST /api/links` request body |
| `LinkCreatedResponse` (incl. `expires_at`) | `POST /api/links` 201 |
| `LinkExpiryUpdate` (`expires_at`, required, nullable; extra fields forbidden) | `PATCH /api/links/{code}` request body |
| `LinkDetailResponse` (incl. `expires_at`, `is_expired`) | `GET /api/links/{code}` 200, `PATCH /api/links/{code}` 200 |
| `LinkStatsResponse`, `DailyClicks`, `ReferrerCount` | `GET /api/links/{code}/stats` 200 |
| `LinkHourlyStatsResponse`, `HourlyClicks` | `GET /api/links/{code}/stats/hourly` 200 |
| `HealthResponse` | `GET /healthz`, `GET /readyz` 200 |
| `ErrorResponse` (`{"detail": any}`) | Documented error responses |

The security scheme `ApiKeyAuth` (`X-API-Key` header) applies to every `/api/links*`
operation.
