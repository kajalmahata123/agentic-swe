# API Reference

URL Shortener 1.1.0. This release adds CR-142 (custom aliases and link expiration)
on top of 1.0.0, and every addition is backwards compatible. See
[CHANGELOG.md](../CHANGELOG.md). This document describes the behaviour in `app/api/*`, `app/schemas/*`,
`app/services/*` and `app/core/errors.py`. FastAPI also serves a machine-readable
schema at `GET /openapi.json`, plus interactive documentation at `GET /docs` (Swagger
UI) and `GET /redoc`.

- **Base URL:** wherever the service is deployed, e.g. `http://localhost:8000`. The
  `short_url` field is built from the `BASE_URL` setting, not from the request host.
- **Content type:** request and response bodies are JSON (`application/json`), apart
  from the empty `204` and `307` responses.
- **Authentication:** every `/api/links*` endpoint requires the `X-API-Key` header.
  Its value must equal the `API_KEY` setting, and the service compares the two in
  constant time. The check fails closed: if `API_KEY` isn't configured, every
  `/api/links*` request is rejected. A missing or wrong key gets
  `401 {"detail": "Invalid or missing API key"}` with a `WWW-Authenticate: ApiKey`
  header. The key is checked before the request body is validated, so a request
  without a valid key always gets `401`, even if its body is invalid. The redirect
  endpoints `GET /{code}` and `HEAD /{code}`, the probes `GET /healthz` and
  `GET /readyz`, and the documentation routes are public. In OpenAPI the scheme is
  called `ApiKeyAuth`.
- **Timestamps:** ISO 8601 in UTC with a `Z` suffix, e.g. `2025-01-15T10:30:00.123456Z`.
  Fractional seconds appear only when they are non-zero.
- **Short codes:** generated codes are base62 (`[A-Za-z0-9]`) and 7 characters long
  by default (`SHORT_CODE_LENGTH`). Clients can choose their own code instead (a
  [custom alias](#custom-aliases), 3–32 characters of `[A-Za-z0-9_-]`). Any `{code}`
  path value that doesn't match `^[A-Za-z0-9_-]{1,32}$` gets `404 Link not found`
  without a database lookup. A well-formed code that doesn't exist also gets `404`.

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
| 401 | `/api/links*` called with a missing or wrong `X-API-Key`, or with no `API_KEY` configured | `"Invalid or missing API key"` |
| 404 | Unknown or malformed short code | `"Link not found"` |
| 404 | No route matches the path (e.g. `/api/links/abc/unknown`) | `"Not Found"` |
| 405 | The route exists but not for this method (e.g. `PUT /api/links/abc`, `GET /api/links`). The response includes an `Allow` header. | `"Method Not Allowed"` |
| 409 | The requested alias is already taken | `"Alias already in use"` |
| 409 | `Idempotency-Key` reused with a different payload, or a concurrent-request conflict | `"Idempotency-Key already used with a different payload"` / `"Idempotency-Key conflict, please retry"` |
| 410 | Redirect to a deactivated link (this takes precedence over expiry) | `"Link has been deactivated"` |
| 410 | Redirect to an expired link | `"Link has expired"` |
| 422 | Request validation failed (body, URL, alias format, `expires_at` format, query parameter) | A list of Pydantic error objects (see below) |
| 422 | The alias is a reserved word | `"Alias is reserved"` |
| 422 | `expires_at` is not in the future | `"expires_at must be in the future"` |
| 500 | Unexpected server error | Always exactly `"Internal Server Error"` |
| 503 | Short-code allocation exhausted | `"Could not allocate a unique short code, please retry"` |
| 503 | Database error on any endpoint | `"Service temporarily unavailable"` |
| 503 | Readiness check failed | `"Database is not ready"` |

Error bodies never contain stack traces, exception class names, exception messages,
SQL or file paths. Server-side failures are logged in full; see
[OPERATIONS.md](OPERATIONS.md).

### 422 validation errors

There are two kinds of `422`:

- **Format and type errors** found while parsing the request (bad URL, bad alias
  format, length or type, `expires_at` that isn't an ISO-8601 string, bad query
  parameter). Here `detail` is the list of Pydantic validation errors.
- **Business-rule errors** found by the service: `"Alias is reserved"` and
  `"expires_at must be in the future"`. Here `detail` is a string.

Each item in the list has `type`, `loc` and `msg`, and usually `input`; some also
have `ctx`. Example for a rejected URL:

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
fields of each list item come from Pydantic and may change between library versions.

---

## POST /api/links

Creates a new active short link. You can pick the code yourself (`alias`) and give
the link an expiry time (`expires_at`).

### Request

Headers:

| Header | Required | Description |
|--------|----------|-------------|
| `X-API-Key` | yes | Must equal the `API_KEY` setting. See [Authentication](#api-reference). |
| `Content-Type: application/json` | yes | |
| `Idempotency-Key` | no | Makes the request idempotent. 1–255 characters and not blank (whitespace-only values are rejected). Keys are global (not tied to a client) and never expire in this release. |

Body (`LinkCreate`):

```json
{"url": "https://example.com/a?b=1"}
```

```json
{"url": "https://example.com/a?b=1", "alias": "my-link_1", "expires_at": "2030-01-01T12:00:00Z"}
```

| Field | Type | Rules |
|-------|------|-------|
| `url` | string (strict: numbers, booleans etc. are not coerced) | Required. See [URL validation rules](#url-validation-rules). Stored and returned **exactly as submitted** (no normalization). |
| `alias` | string (strict) or `null` | Optional. See [Custom aliases](#custom-aliases). If you omit it or send `null`, the service generates a code. |
| `expires_at` | string (ISO-8601 date-time) or `null` | Optional. See [Link expiration](#link-expiration). If you omit it or send `null`, the link never expires. |

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

These rules apply whether or not the request also sends `alias` or `expires_at`.
Neither field skips URL validation.

| Example | Result |
|---------|--------|
| `https://example.com/a?b=1` | 201 |
| `HTTPS://example.com`, `HTTP://Example.com`, `HTTPS://EXAMPLE.COM/Path` | 201, stored and redirected byte-for-byte as sent |
| `http://localhost:8080/x` | 201 |
| `""` (empty) | 422 `URL must not be empty` |
| 2049+ characters | 422 `URL must be at most 2048 characters` |
| `http://exa mple.com`, a URL containing `\n` or `\t`, `https://bücher.de` | 422 `URL must not contain whitespace, control or non-ASCII characters` |
| `" javascript:alert(1)"`, `"\tjavascript:alert(1)"`, `"java\nscript:alert(1)"`, a URL with leading or trailing whitespace or newlines | 422 `URL must not contain whitespace, control or non-ASCII characters` |
| `http://example.com:99999` | 422 `invalid URL` |
| `javascript:alert(1)`, `JavaScript:alert(1)`, `JAVASCRIPT:alert(1)`, `javascript://example.com/%0aalert(1)`, `data:text/html,hi`, `file:///etc/passwd`, `ftp://example.com`, `mailto:a@b.c` | 422 `URL scheme must be http or https` |
| `http:javascript:alert(1)`, `/path`, `example.com` | 422 (not an absolute `http`/`https` URL) |
| `http://`, `https:///x` | 422 `URL must include a host` |

When validation fails, nothing is stored.

#### Custom aliases

- **Format:** 3–32 characters, each one of `A-Z`, `a-z`, `0-9`, `_` or `-`
  (`^[A-Za-z0-9_-]{3,32}$`). The value must be a JSON string. Numbers, booleans,
  arrays and the empty string are rejected. Anything that breaks the format
  (e.g. `ab`, 33 characters, `a.b`, `a b`, `a/b`, `ä-link`, `abc%20`) gets `422`
  with a list `detail` and nothing is stored.
- **Case-sensitive:** the alias is stored exactly as sent, as the link's `code`.
  `MyLink` and `mylink` are two different links. This doesn't hold on MySQL with its
  default case-insensitive collation; see [OPERATIONS.md](OPERATIONS.md#data-model-retention-and-privacy).
- **Reserved words** are rejected case-insensitively with
  `422 {"detail": "Alias is reserved"}`. The list is shared with generated codes, which
  never match these words either:

  `api`, `docs`, `redoc`, `healthz`, `readyz`, `metrics`, `openapi.json`, `static`,
  `favicon.ico`

  For example `API`, `Docs` and `METRICS` are rejected, while `api-docs` and
  `my-metrics` are accepted. (`openapi.json` and `favicon.ico` can't pass the alias
  format anyway. They are listed because generated codes and aliases share one list.)
- **Uniqueness:** an alias that is already used as a code gets
  `409 {"detail": "Alias already in use"}`. This includes generated codes and
  deactivated or expired links: an alias is never released. If two requests race for
  the same alias, exactly one gets `201` and the others get `409`.
- **Immutable:** there is no endpoint to change or remove the alias of an existing link.
- `SHORT_CODE_LENGTH` and `CODE_MAX_ATTEMPTS` apply only to generated codes, not to
  aliases.

#### Link expiration

- **Format:** an ISO-8601 date-time string,
  `YYYY-MM-DD[T ]HH:MM[:SS[.ffffff]][Z|±HH:MM]`, e.g. `2030-01-01T12:00:00Z`,
  `2030-01-01T14:00:00+02:00` or `2030-01-01 12:00`. Values without an offset are
  treated as UTC. Anything else gets `422` with a list `detail`: numbers (including
  Unix timestamps), booleans, `""`, `"tomorrow"`, date-only values such as
  `"2030-01-01"`, and impossible dates such as month 13.
- **Must be in the future:** the instant must be strictly later than the server's
  current UTC time. Otherwise the response is
  `422 {"detail": "expires_at must be in the future"}`. There is no upper limit.
- **Effect:** once the current time reaches `expires_at` (from that exact instant
  onward), `GET /{code}` and `HEAD /{code}` return `410 {"detail": "Link has expired"}`.
  They send no `Location` and record no click. If the link is also deactivated,
  `"Link has been deactivated"` takes precedence. Metadata, stats and `DELETE` keep
  working for expired links. Expiry doesn't change `is_active`, which stays `true`
  until the link is deactivated. Expired links are never deleted automatically.
- **Immutable:** you can't change or remove `expires_at` after creation. To end a
  link early, deactivate it with `DELETE /api/links/{code}`.
- Responses always return `expires_at` normalised to UTC with a `Z` suffix. For
  example, `2030-01-01T14:00:00+02:00` is returned as `2030-01-01T12:00:00Z`.

#### Order of checks

1. `X-API-Key` (`401`).
2. Body format and types: `url`, `alias`, `expires_at` (`422` with a list `detail`).
3. `Idempotency-Key` header format (`400`).
4. Idempotency lookup. A known key replays the original link or returns `409` (see
   [Idempotency semantics](#idempotency-semantics)). This runs before the checks
   below, so retrying a request that already succeeded always replays, even after
   its `expires_at` has passed.
5. Reserved alias (`422 "Alias is reserved"`).
6. `expires_at` in the future (`422 "expires_at must be in the future"`).
7. Alias uniqueness (`409 "Alias already in use"`).
8. Insert. For generated codes, collisions are retried up to `CODE_MAX_ATTEMPTS`
   times (`503` when exhausted).

### Responses

**201 Created**: `LinkCreatedResponse`

Headers:

- `Location: /api/links/{code}` (a relative path to the metadata resource)
- `Idempotent-Replayed: true` only when the response replays an earlier request with
  the same `Idempotency-Key`. It is absent on the first response.

```json
{
  "code": "my-link_1",
  "short_url": "http://localhost:8000/my-link_1",
  "url": "https://example.com/a?b=1",
  "is_active": true,
  "created_at": "2025-01-15T10:30:00.123456Z",
  "expires_at": "2030-01-01T12:00:00Z",
  "is_expired": false
}
```

| Field | Type | Description |
|-------|------|-------------|
| `code` | string | The short code. Equals `alias` when one was sent. Otherwise it is a generated base62 code (7 characters by default). |
| `short_url` | string | `BASE_URL` with trailing `/` stripped, then `/`, then `code`. |
| `url` | string | The original URL, unchanged. |
| `is_active` | boolean | `true` for a new link. On a replay, this reflects the link's current state. |
| `created_at` | string (date-time, UTC) | Creation time. |
| `expires_at` | string (date-time, UTC, `Z`) or `null` | Expiry time. `null` means the link never expires. |
| `is_expired` | boolean | `true` once the current server time has reached `expires_at`. It is always `false` when `expires_at` is `null`. On a replay, this reflects the current time. |

| Status | Body | When |
|--------|------|------|
| 400 | `{"detail": "Invalid Idempotency-Key header"}` | `Idempotency-Key` is empty, whitespace-only or longer than 255 characters. |
| 401 | `{"detail": "Invalid or missing API key"}` | `X-API-Key` is missing or wrong, or `API_KEY` isn't configured. |
| 409 | `{"detail": "Alias already in use"}` | The alias is already taken by any link (active, deactivated or expired), or another request inserted it at the same moment. No link is created. |
| 409 | `{"detail": "Idempotency-Key already used with a different payload"}` | The key was already used with a different `url`, `alias` or `expires_at`. No link is created. |
| 409 | `{"detail": "Idempotency-Key conflict, please retry"}` | Rare: a concurrent request with the same key could not be resolved. Retrying is safe. |
| 422 | `{"detail": [...]}` | Missing or invalid JSON body, missing `url`, non-string `url`, a URL that breaks the validation rules, a badly formatted or non-string `alias`, or an `expires_at` that isn't an ISO-8601 date-time string. |
| 422 | `{"detail": "Alias is reserved"}` | The alias matches a reserved word (case-insensitive). |
| 422 | `{"detail": "expires_at must be in the future"}` | `expires_at` is at or before the current server time. |
| 503 | `{"detail": "Could not allocate a unique short code, please retry"}` | All `CODE_MAX_ATTEMPTS` (default 5) generated codes collided or were reserved. This is extremely unlikely in practice and never happens on the alias path. |
| 503 | `{"detail": "Service temporarily unavailable"}` | Database error. |

### Idempotency semantics

| Situation | Result |
|-----------|--------|
| No `Idempotency-Key` | A new link is created on every request. Identical URLs are **not** deduplicated. |
| New key | A link is created and the key is stored in the same transaction. |
| Same key, same payload | `201` with the original link (same `code`) plus `Idempotent-Replayed: true`. No new row is created. |
| Same key, different payload (`url`, `alias` or `expires_at` differs, or one of them was added or removed) | `409`. Nothing is created. |
| Different keys, same payload | Separate links are created. With the same `alias`, the second request gets `409 Alias already in use`. |
| Concurrent requests with the same key | A database unique constraint guarantees that at most one link is created. The other request gets a replay or a `409`, never a `500`. |
| A request with a key fails (4xx or 5xx) | The key is not stored, so the client can fix the request and retry with the same key. |

Keys are compared byte-for-byte (case-sensitive). The payload is compared by a
SHA-256 hash stored as `request_hash` (lower-case hex):

- If both `alias` and `expires_at` are absent or `null`:
  `request_hash = sha256(url)`, using the UTF-8 bytes of the `url` string. This is
  exactly the hash that 1.0.0 used, so keys stored by earlier versions keep
  replaying.
- Otherwise: `request_hash` is the SHA-256 of this canonical JSON (UTF-8, keys
  sorted, no whitespace, separators `,` and `:`):

  ```json
  {"alias":"my-link_1","expires_at":"2030-01-01T12:00:00.000000Z","url":"https://example.com/a?b=1"}
  ```

  `alias` is the alias or `null`. `expires_at` is the instant converted to UTC and
  formatted as `YYYY-MM-DDTHH:MM:SS.ffffffZ`, or `null`. So the same instant sent with
  different offsets (`2030-01-01T12:00:00Z`, `2030-01-01T14:00:00+02:00`,
  `2030-01-01 12:00`) counts as the same payload.

A replay returns the link's **current** state: `is_active`, `expires_at` and
`is_expired` as they are now, not as they were when the link was created.

### Examples

The examples read the key from the `API_KEY` environment variable of your shell.

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

{"code":"aB3xY9Q","short_url":"http://localhost:8000/aB3xY9Q","url":"https://example.com/a?b=1","is_active":true,"created_at":"2025-01-15T10:30:00.123456Z","expires_at":null,"is_expired":false}
```

Custom alias with an expiry:

```bash
curl -i -X POST http://localhost:8000/api/links \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/launch", "alias": "launch-2030", "expires_at": "2030-01-01T14:00:00+02:00"}'
```

```
HTTP/1.1 201 Created
location: /api/links/launch-2030

{"code":"launch-2030","short_url":"http://localhost:8000/launch-2030","url":"https://example.com/launch","is_active":true,"created_at":"2025-01-15T10:30:00Z","expires_at":"2030-01-01T12:00:00Z","is_expired":false}
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

{"code":"Zk81mPq", ...same body as the first response, with the current state...}
```

Errors:

```bash
# Invalid URL
curl -s -X POST http://localhost:8000/api/links -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' -d '{"url": "ftp://example.com"}'
# 422 {"detail":[{"type":"value_error","loc":["body","url"],"msg":"Value error, URL scheme must be http or https",...}]}

# Reserved alias
curl -s -X POST http://localhost:8000/api/links -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' -d '{"url": "https://example.com", "alias": "Metrics"}'
# 422 {"detail":"Alias is reserved"}

# Alias already taken
# 409 {"detail":"Alias already in use"}

# Expiry in the past
curl -s -X POST http://localhost:8000/api/links -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' -d '{"url": "https://example.com", "expires_at": "2020-01-01T00:00:00Z"}'
# 422 {"detail":"expires_at must be in the future"}

# Missing API key
curl -s -X POST http://localhost:8000/api/links \
  -H 'Content-Type: application/json' -d '{"url": "https://example.com"}'
# 401 {"detail":"Invalid or missing API key"}
```

---

## GET /api/links/{code}

Returns metadata for a link, whether it is active, deactivated or expired.
Requires `X-API-Key`.

### Request

| Parameter | In | Description |
|-----------|----|-------------|
| `code` | path | Short code or alias (`^[A-Za-z0-9_-]{1,32}$`). |

### Responses

**200 OK**: `LinkDetailResponse`

```json
{
  "code": "aB3xY9Q",
  "short_url": "http://localhost:8000/aB3xY9Q",
  "url": "https://example.com/a?b=1",
  "is_active": true,
  "created_at": "2025-01-15T10:30:00.123456Z",
  "expires_at": null,
  "is_expired": false,
  "deactivated_at": null,
  "total_clicks": 42
}
```

This response has all the fields of `LinkCreatedResponse` (including `expires_at` and
`is_expired`), plus:

| Field | Type | Description |
|-------|------|-------------|
| `deactivated_at` | string (date-time, UTC) or `null` | When the link was deactivated. `null` while it is active. |
| `total_clicks` | integer | All-time number of recorded clicks. |

For links created before 1.1.0 (CR-142), `expires_at` is `null` and `is_expired` is `false`.

| Status | Body | When |
|--------|------|------|
| 401 | `{"detail": "Invalid or missing API key"}` | Missing or wrong `X-API-Key`. |
| 404 | `{"detail": "Link not found"}` | Unknown code, or a code containing characters outside `[A-Za-z0-9_-]` or longer than 32 characters. |
| 503 | `{"detail": "Service temporarily unavailable"}` | Database error. |

### Example

```bash
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/api/links/aB3xY9Q
```

---

## DELETE /api/links/{code}

Deactivates (soft-deletes) a link. The row and its click history are kept, metadata
and stats stay available, and redirects return `410`. Calling it again has no further
effect. This works for expired links and for aliases too. Requires `X-API-Key`.

### Responses

| Status | Body | When |
|--------|------|------|
| 204 | *(empty)* | The link is now inactive. On the first call, `is_active` becomes `false` and `deactivated_at` is set to the current UTC time. Later calls also return `204` and leave `deactivated_at` unchanged. |
| 401 | `{"detail": "Invalid or missing API key"}` | Missing or wrong `X-API-Key`. |
| 404 | `{"detail": "Link not found"}` | Unknown or malformed code. |
| 503 | `{"detail": "Service temporarily unavailable"}` | Database error. |

You can't reactivate a link or delete it permanently in this release. A deactivated
link's code or alias stays taken.

### Example

```bash
curl -i -X DELETE -H "X-API-Key: $API_KEY" http://localhost:8000/api/links/aB3xY9Q
# HTTP/1.1 204 No Content
```

---

## GET /api/links/{code}/stats

Returns aggregated click statistics. Works for active, deactivated and expired links.
Requires `X-API-Key`.

### Request

| Parameter | In | Type | Default | Description |
|-----------|----|------|---------|-------------|
| `code` | path | string | n/a | Short code or alias (`^[A-Za-z0-9_-]{1,32}$`). |
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
| 401 | `{"detail": "Invalid or missing API key"}` | Missing or wrong `X-API-Key`. |
| 404 | `{"detail": "Link not found"}` | Unknown or malformed code. |
| 422 | `{"detail": [...]}` | `days` is not an integer or is outside 1–365 (e.g. `days=0`, `days=366`). |
| 503 | `{"detail": "Service temporarily unavailable"}` | Database error. |

### Example

```bash
curl -s -H "X-API-Key: $API_KEY" 'http://localhost:8000/api/links/aB3xY9Q/stats?days=7'
```

---

## GET /{code}

Redirects to the original URL and records a click event. Public: no API key needed.

### Request

| Parameter | In | Description |
|-----------|----|-------------|
| `code` | path | Short code or alias (`^[A-Za-z0-9_-]{1,32}$`). |
| `Referer` | header (optional) | Stored on the click event, truncated to 2048 characters. A missing or empty header is stored as `null`. |
| `User-Agent` | header (optional) | Stored on the click event, truncated to 512 characters. A missing or empty header is stored as `null`. |

Oversized `Referer`/`User-Agent` values are truncated and never cause an error. The
client IP is **never** stored. If `IP_HASH_SALT` is configured, the service stores
an HMAC-SHA256 hash of the client address as reported by the ASGI server. The app
itself ignores `X-Forwarded-For`.

### Responses

| Status | Headers / Body | When |
|--------|----------------|------|
| 307 Temporary Redirect | `Location: <original URL, byte-for-byte as stored>`, `Cache-Control: no-store`. Empty body. | The link is active and not expired. One click event is recorded. |
| 404 | `{"detail": "Link not found"}` | Unknown or malformed code. No click is recorded. |
| 410 Gone | `{"detail": "Link has been deactivated"}` | The link has been deactivated. This takes precedence over expiry. No click is recorded and there is no redirect. |
| 410 Gone | `{"detail": "Link has expired"}` | The link is active but its `expires_at` is at or before the current time. No click is recorded and there is no redirect. |
| 503 | `{"detail": "Service temporarily unavailable"}` | The database is unavailable while looking up the link. |

Click recording is best-effort. If writing the click event fails, the error is logged
and the client still gets the `307`. Checking for expiry adds no database query: it
uses the link row that was already loaded.

Fixed routes (`/healthz`, `/readyz`, `/docs`, `/redoc`, `/openapi.json`, `/api/...`)
are registered before this catch-all route, so it never shadows them. Neither
generated codes nor aliases can match a reserved word (see
[Custom aliases](#custom-aliases)).

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
(`307`, `404` or `410`, with the same deactivated-before-expired precedence) but no
body. It does **not** record a click. Public: no API key needed. This endpoint is
listed in the OpenAPI schema with its `307`, `404` and `410` responses.

```bash
curl -I http://localhost:8000/aB3xY9Q
```

---

## GET /healthz

Liveness probe. It does not touch the database. Public.

**200 OK**: `HealthResponse`

```json
{"status": "ok"}
```

---

## GET /readyz

Readiness probe. It runs `SELECT 1` on a database session. Public.

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
| `LinkCreate` (`url`, optional `alias`, optional `expires_at`) | `POST /api/links` request body |
| `LinkCreatedResponse` (includes `expires_at`, `is_expired`) | `POST /api/links` 201 |
| `LinkDetailResponse` (includes `expires_at`, `is_expired`) | `GET /api/links/{code}` 200 |
| `LinkStatsResponse`, `DailyClicks`, `ReferrerCount` | `GET /api/links/{code}/stats` 200 |
| `HealthResponse` | `GET /healthz`, `GET /readyz` 200 |
| `ErrorResponse` (`{"detail": any}`) | Documented error responses (401, 404, 409, 410, 422, ...) |

The `ApiKeyAuth` security scheme (an `X-API-Key` header) applies to every
`/api/links*` operation.
