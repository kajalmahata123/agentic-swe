# API Reference

URL Shortener 1.0.0. This document describes the behaviour in `app/api/*`,
`app/schemas/*` and `app/core/errors.py`. FastAPI also serves a machine-readable
schema at `GET /openapi.json`, plus interactive documentation at `GET /docs` (Swagger
UI) and `GET /redoc`.

- **Base URL:** wherever the service is deployed, e.g. `http://localhost:8000`. The
  `short_url` field is built from the `BASE_URL` setting, not from the request host.
- **Content type:** request and response bodies are JSON (`application/json`), apart
  from the empty `204` and `307` responses.
- **Authentication:** none in this release.
- **Timestamps:** ISO 8601 in UTC with a `Z` suffix, e.g. `2025-01-15T10:30:00.123456Z`.
  Fractional seconds appear only when they are non-zero.
- **Short codes:** generated codes are base62 (`[A-Za-z0-9]`) and 7 characters long
  by default (`SHORT_CODE_LENGTH`). Any `{code}` path value that doesn't match
  `^[A-Za-z0-9]{1,32}$` gets `404 Link not found` without a database lookup.

## Contents

- [Error format](#error-format)
- [POST /api/links](#post-apilinks): create a short link
- [GET /api/links/{code}](#get-apilinkscode): link metadata
- [DELETE /api/links/{code}](#delete-apilinkscode): deactivate a link
- [GET /api/links/{code}/stats](#get-apilinkscodestats): click stats
- [GET /{code}](#get-code): redirect
- [HEAD /{code}](#head-code): redirect check without recording a click
- [GET /healthz](#get-healthz): liveness probe
- [GET /readyz](#get-readyz): readiness probe

---

## Error format

Every error response is JSON with a single `detail` key:

```json
{"detail": "Link not found"}
```

| Status | When | `detail` |
|--------|------|----------|
| 400 | Invalid `Idempotency-Key` header | `"Invalid Idempotency-Key header"` |
| 404 | Unknown or malformed short code | `"Link not found"` |
| 404 | No route matches the path (e.g. `/api/links/abc/unknown`) | `"Not Found"` |
| 405 | The route exists but not for this method (e.g. `PUT /api/links/abc`, `GET /api/links`). The response includes an `Allow` header. | `"Method Not Allowed"` |
| 409 | `Idempotency-Key` reused with a different URL, or a concurrent-request conflict | `"Idempotency-Key already used with a different payload"` / `"Idempotency-Key conflict, please retry"` |
| 410 | Redirect to a deactivated link | `"Link has been deactivated"` |
| 422 | Request validation failed (body, URL, query parameter) | A list of Pydantic error objects (see below) |
| 500 | Unexpected server error | Always exactly `"Internal Server Error"` |
| 503 | Short-code allocation exhausted | `"Could not allocate a unique short code, please retry"` |
| 503 | Database error on any endpoint | `"Service temporarily unavailable"` |
| 503 | Readiness check failed | `"Database is not ready"` |

Error bodies never contain stack traces, exception class names, exception messages,
SQL or file paths. Server-side failures are logged in full; see
[OPERATIONS.md](OPERATIONS.md).

### 422 validation errors

For a `422`, `detail` is the list of Pydantic validation errors. Each item has
`type`, `loc`, `msg` and `input`, and some also have `ctx`. Example for a rejected URL:

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

Clients should rely only on the status code and the presence of `detail`. The exact
fields of each item come from Pydantic and may change between library versions.

---

## POST /api/links

Creates a new active short link.

### Request

Headers:

| Header | Required | Description |
|--------|----------|-------------|
| `Content-Type: application/json` | yes | |
| `Idempotency-Key` | no | Makes the request idempotent. 1–255 characters and not blank (whitespace-only values are rejected). Keys are global (not tied to a client) and never expire in this release. |

Body (`LinkCreate`):

```json
{"url": "https://example.com/a?b=1"}
```

| Field | Type | Rules |
|-------|------|-------|
| `url` | string (strict: numbers, booleans etc. are not coerced) | See [URL validation rules](#url-validation-rules). Stored and returned **exactly as submitted** (no normalization). |

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
  "url": "https://example.com/a?b=1",
  "is_active": true,
  "created_at": "2025-01-15T10:30:00.123456Z"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `code` | string | The short code. |
| `short_url` | string | `BASE_URL` with trailing `/` stripped, then `/`, then `code`. |
| `url` | string | The original URL, unchanged. |
| `is_active` | boolean | `true` for a new link. On a replay, this reflects the link's current state. |
| `created_at` | string (date-time, UTC) | Creation time. |

| Status | Body | When |
|--------|------|------|
| 400 | `{"detail": "Invalid Idempotency-Key header"}` | `Idempotency-Key` is empty, whitespace-only or longer than 255 characters. |
| 409 | `{"detail": "Idempotency-Key already used with a different payload"}` | The key was already used with a different `url`. No link is created. |
| 409 | `{"detail": "Idempotency-Key conflict, please retry"}` | Rare: a concurrent request with the same key could not be resolved. Retrying is safe. |
| 422 | `{"detail": [...]}` | Missing or invalid JSON body, missing `url`, non-string `url`, or a URL that breaks the validation rules. |
| 503 | `{"detail": "Could not allocate a unique short code, please retry"}` | All `CODE_MAX_ATTEMPTS` (default 5) generated codes collided or were reserved. This is extremely unlikely in practice. |
| 503 | `{"detail": "Service temporarily unavailable"}` | Database error. |

### Idempotency semantics

| Situation | Result |
|-----------|--------|
| No `Idempotency-Key` | A new link is created on every request. Identical URLs are **not** deduplicated. |
| New key | A link is created and the key is stored in the same transaction. |
| Same key, same `url` | `201` with the original link (same `code`) plus `Idempotent-Replayed: true`. No new row is created. |
| Same key, different `url` | `409`. Nothing is created. |
| Different keys, same `url` | Separate links are created. |
| Concurrent requests with the same key | A database unique constraint guarantees that at most one link is created. The other request gets a replay or a `409`, never a `500`. |

Keys are compared byte-for-byte (case-sensitive). The payload is compared by the
SHA-256 hash of the `url` string.

### Examples

```bash
curl -i -X POST http://localhost:8000/api/links \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/a?b=1"}'
```

```
HTTP/1.1 201 Created
content-type: application/json
location: /api/links/aB3xY9Q

{"code":"aB3xY9Q","short_url":"http://localhost:8000/aB3xY9Q","url":"https://example.com/a?b=1","is_active":true,"created_at":"2025-01-15T10:30:00.123456Z"}
```

Idempotent replay (the second identical request):

```bash
curl -i -X POST http://localhost:8000/api/links \
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

Invalid URL:

```bash
curl -s -X POST http://localhost:8000/api/links \
  -H 'Content-Type: application/json' -d '{"url": "ftp://example.com"}'
# 422 {"detail":[{"type":"value_error","loc":["body","url"],"msg":"Value error, URL scheme must be http or https",...}]}
```

---

## GET /api/links/{code}

Returns metadata for a link, whether it is active or deactivated.

### Request

| Parameter | In | Description |
|-----------|----|-------------|
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
  "deactivated_at": null,
  "total_clicks": 42
}
```

This response has all the fields of `LinkCreatedResponse`, plus:

| Field | Type | Description |
|-------|------|-------------|
| `deactivated_at` | string (date-time, UTC) or `null` | When the link was deactivated. `null` while it is active. |
| `total_clicks` | integer | All-time number of recorded clicks. |

| Status | Body | When |
|--------|------|------|
| 404 | `{"detail": "Link not found"}` | Unknown code, or a code containing characters outside `[A-Za-z0-9]` or longer than 32 characters. |
| 503 | `{"detail": "Service temporarily unavailable"}` | Database error. |

### Example

```bash
curl -s http://localhost:8000/api/links/aB3xY9Q
```

---

## DELETE /api/links/{code}

Deactivates (soft-deletes) a link. The row and its click history are kept, metadata
and stats stay available, and redirects return `410`. Calling it again has no further
effect.

### Responses

| Status | Body | When |
|--------|------|------|
| 204 | *(empty)* | The link is now inactive. On the first call, `is_active` becomes `false` and `deactivated_at` is set to the current UTC time. Later calls also return `204` and leave `deactivated_at` unchanged. |
| 404 | `{"detail": "Link not found"}` | Unknown or malformed code. |
| 503 | `{"detail": "Service temporarily unavailable"}` | Database error. |

You can't reactivate a link or delete it permanently in this release.

### Example

```bash
curl -i -X DELETE http://localhost:8000/api/links/aB3xY9Q
# HTTP/1.1 204 No Content
```

---

## GET /api/links/{code}/stats

Returns aggregated click statistics. Works for both active and deactivated links.

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
| 404 | `{"detail": "Link not found"}` | Unknown or malformed code. |
| 422 | `{"detail": [...]}` | `days` is not an integer or is outside 1–365 (e.g. `days=0`, `days=366`). |
| 503 | `{"detail": "Service temporarily unavailable"}` | Database error. |

### Example

```bash
curl -s 'http://localhost:8000/api/links/aB3xY9Q/stats?days=7'
```

---

## GET /{code}

Redirects to the original URL and records a click event.

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

### Responses

| Status | Headers / Body | When |
|--------|----------------|------|
| 307 Temporary Redirect | `Location: <original URL, byte-for-byte as stored>`, `Cache-Control: no-store`. Empty body. | The link is active. One click event is recorded. |
| 404 | `{"detail": "Link not found"}` | Unknown or malformed code. No click is recorded. |
| 410 Gone | `{"detail": "Link has been deactivated"}` | The link has been deactivated. No click is recorded and there is no redirect. |
| 503 | `{"detail": "Service temporarily unavailable"}` | The database is unavailable while looking up the link. |

Click recording is best-effort. If writing the click event fails, the error is logged
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

---

## HEAD /{code}

Returns the same status and `Location` / `Cache-Control` headers as `GET /{code}`
(`307`, `404` or `410`) with no body. It does **not** record a click. This endpoint
is not listed in the OpenAPI schema.

```bash
curl -I http://localhost:8000/aB3xY9Q
```

---

## GET /healthz

Liveness probe. It does not touch the database.

**200 OK**: `HealthResponse`

```json
{"status": "ok"}
```

---

## GET /readyz

Readiness probe. It runs `SELECT 1` on a database session.

| Status | Body | When |
|--------|------|------|
| 200 | `{"status": "ready"}` | The database query succeeded. |
| 503 | `{"detail": "Database is not ready"}` | The query failed. |
| 503 | `{"detail": "Service temporarily unavailable"}` | A database error was raised before the query could run, e.g. while opening the session. |

The body never contains exception text.

---

## Schemas

These named schemas appear in `/openapi.json`:

| Schema | Used by |
|--------|---------|
| `LinkCreate` | `POST /api/links` request body |
| `LinkCreatedResponse` | `POST /api/links` 201 |
| `LinkDetailResponse` | `GET /api/links/{code}` 200 |
| `LinkStatsResponse`, `DailyClicks`, `ReferrerCount` | `GET /api/links/{code}/stats` 200 |
| `HealthResponse` | `GET /healthz`, `GET /readyz` 200 |
| `ErrorResponse` (`{"detail": any}`) | Documented error responses |
