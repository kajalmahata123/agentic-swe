# URL Shortener

An HTTP service for creating short links to long `http`/`https` URLs. Visiting a
short link redirects to the original URL with **HTTP 307**. Every redirect is
recorded as a click event that **never stores the raw client IP**, and each link has
its own stats (total clicks, clicks per UTC day, top referrers).

Built with Python 3.11, FastAPI, SQLAlchemy 2.0 (synchronous ORM) and Pydantic v2.
It uses SQLite by default and runs on PostgreSQL or MySQL if you change `DATABASE_URL`.

**Current release: 1.1.0.** It adds custom aliases, link expiration and the
`links.expires_at` column. The column is added automatically at startup; see
[Upgrading from 1.0.0](#upgrading-from-100) and [CHANGELOG.md](CHANGELOG.md).

> **Security notice:** all management endpoints (`/api/links*`: create, metadata,
> stats, deactivate) require an `X-API-Key` header that matches the `API_KEY`
> setting. A missing or wrong key gets `401 {"detail": "Invalid or missing API key"}`.
> The check **fails closed**: if `API_KEY` isn't set, every `/api/links*` request is
> rejected. Redirects (`GET/HEAD /{code}`), the probes and the API docs are public.
> Rate limiting is not built in, so put the service behind a gateway that provides it
> before you expose it publicly. See
> [docs/OPERATIONS.md](docs/OPERATIONS.md#security-notes).

## Features

- **API-key protected management:** every `/api/links*` request needs
  `X-API-Key: <API_KEY>`. The key is compared in constant time, and requests are
  refused when no key is configured.
- **Create short links:** `POST /api/links` with `{"url": "..."}`. The URL is stored
  exactly as submitted.
- **Custom aliases:** send `"alias": "my-link_1"` to choose the code yourself. Aliases
  are 3–32 characters of `[A-Za-z0-9_-]`, case-sensitive and immutable. A taken alias
  gets `409`, and an invalid alias gets `422`.
- **Link expiration:** send `"expires_at": "2030-01-01T12:00:00Z"` (ISO-8601; values
  without an offset are UTC; must be in the future). After that instant, redirects
  return `410 Gone` with `"Link has expired"`. Metadata and stats stay available, and
  responses include `expires_at` and `is_expired`.
- **Reserved words:** `api`, `docs`, `redoc`, `healthz`, `readyz`, `metrics`,
  `openapi.json`, `static` and `favicon.ico` (case-insensitive) are never issued as
  codes and are rejected as aliases with `422`.
- **Strict URL validation:** only absolute `http`/`https` URLs with a host are
  accepted, up to 2048 characters. URLs with whitespace, control or non-ASCII
  characters are rejected, as are `javascript:`, `data:`, `file:`, `ftp:` and
  `mailto:` URLs and relative URLs. To use an internationalised URL, send it in
  punycode or percent-encoded form.
- **Random, collision-safe codes:** 7 base62 characters (`[A-Za-z0-9]`) from Python's
  `secrets` CSPRNG. A database unique constraint backs the codes. On a collision the
  service retries, up to 5 attempts by default. Codes that match a reserved word are
  never issued.
- **Idempotent creation:** send an `Idempotency-Key` header and a retried request
  returns the original link with `Idempotent-Replayed: true` instead of creating a
  duplicate. A database unique constraint enforces this, so it also works across
  concurrent requests and multiple instances.
- **307 redirects** (`GET /{code}`) with `Cache-Control: no-store`, so every visit is
  counted and deactivation takes effect immediately. `HEAD /{code}` is also supported
  and does not count as a click.
- **Soft delete:** `DELETE /api/links/{code}` deactivates a link and keeps its row
  and click history. Redirects to a deactivated link return `410 Gone` with
  `"Link has been deactivated"`, which takes precedence over expiry.
- **Privacy-preserving click analytics:** each click stores the timestamp, `Referer`
  and `User-Agent`. The raw IP is never stored. If `IP_HASH_SALT` is set, the service
  stores an HMAC-SHA256 hash of the IP instead.
- **Stats:** all-time total clicks, clicks per UTC day for a configurable window
  (default 30 days) and the top 10 referrers.
- **Probes:** `/healthz` (liveness, does not use the DB) and `/readyz` (readiness,
  runs `SELECT 1`).
- **Consistent JSON errors:** every error body has the form `{"detail": ...}`. Stack
  traces and exception messages are never returned to clients.
- **Structured JSON logs:** one line per request with method, path, status and
  duration. Logs never contain client IPs or query strings.
- **OpenAPI docs** at `/docs`, `/redoc` and `/openapi.json`.

## Quick start

Requires Python 3.11 or later.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# The management API needs a key of at least 16 characters (without one, /api/links* returns 401).
export API_KEY='change-me-to-a-long-random-secret'

# Run the service. --no-access-log is required: uvicorn's access log prints client IPs.
uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log
```

When the service starts, it creates any missing tables (`links`, `click_events`,
`idempotency_keys`) in the configured database. By default that is the file
`./shortener.db`. It then runs an idempotent migration that adds the
`links.expires_at` column to databases created before CR-142. See
[docs/OPERATIONS.md](docs/OPERATIONS.md#schema-migration-linksexpires_at) for the
manual SQL and rollout advice.

Open http://localhost:8000/docs for the interactive API documentation.

### Upgrading from 1.0.0

Version 1.1.0 needs the new nullable column `links.expires_at`. On startup the service
adds it with `ALTER TABLE links ADD COLUMN expires_at DATETIME NULL` (on PostgreSQL:
`TIMESTAMP NULL`). This step is idempotent and logs one INFO line on `app.migrations`.
Existing links get `NULL` and never expire. For multi-instance deployments, migrate
once before the rollout: run the SQL manually or start a single instance first.
Rolling back to 1.0.0 is safe without dropping the column. Details are in
[docs/OPERATIONS.md](docs/OPERATIONS.md#schema-migration-linksexpires_at).

### Running the tests

```bash
python -m pytest -q
python -m pytest -q --cov=app --cov-report=term-missing   # with coverage
```

Each test runs against its own temporary SQLite database, because the app factory
`create_app(Settings(database_url=...))` creates a separate engine per app instance.
The tests don't need any external services.

## Configuration

All settings come from environment variables. Names are case-insensitive, and no
`.env` file is read.

| Variable            | Default                    | Description |
|---------------------|----------------------------|-------------|
| `DATABASE_URL`      | `sqlite:///./shortener.db` | SQLAlchemy database URL. For SQLite, `check_same_thread=False` is applied automatically. Other backends get `pool_pre_ping=True`. PostgreSQL and MySQL also need a driver that is **not** in `requirements.txt`, e.g. `psycopg[binary]` for `postgresql+psycopg://...` or `pymysql` for `mysql+pymysql://...`. |
| `BASE_URL`          | `http://localhost:8000`    | Public base URL used to build `short_url`. A trailing `/` is stripped. |
| `SHORT_CODE_LENGTH` | `7`                        | Length of newly generated codes. Integer, 4–32. |
| `CODE_MAX_ATTEMPTS` | `5`                        | Number of code-generation attempts before `POST /api/links` returns 503. Integer, 1–20. |
| `IP_HASH_SALT`      | *(unset)*                  | Secret key for hashing client IPs (HMAC-SHA256) into `click_events.ip_hash`. If unset, empty or whitespace-only, `ip_hash` is `NULL`. Raw IPs are never stored either way. |
| `LOG_LEVEL`         | `INFO`                     | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` (case-insensitive). Applies to the `app.*` loggers. |
| `API_KEY`           | *(unset)*                  | Shared secret that must be sent as `X-API-Key` on every `/api/links*` request. At least 16 characters. If it is unset, empty or whitespace-only, all `/api/links*` requests get `401` (fail closed). |

If a value is invalid (for example `SHORT_CODE_LENGTH=2`, `LOG_LEVEL=verbose` or an
`API_KEY` shorter than 16 characters), the application fails at startup with a
validation error.

## API overview

Full reference: [docs/API.md](docs/API.md).

| Method   | Path                      | Description                                  | Success | Errors |
|----------|---------------------------|----------------------------------------------|---------|--------|
| `POST`   | `/api/links`              | Create a short link (optional `alias`, `expires_at`, `Idempotency-Key`) | 201 | 400, 401, 409, 422, 503 |
| `GET`    | `/api/links/{code}`       | Get link metadata (including deactivated and expired links) | 200 | 401, 404 |
| `DELETE` | `/api/links/{code}`       | Deactivate (soft-delete) a link, idempotent  | 204     | 401, 404 |
| `GET`    | `/api/links/{code}/stats` | Click stats (`?days=1..365`, default 30)     | 200     | 401, 404, 422 |
| `GET`    | `/{code}`                 | Redirect to the original URL and record a click | 307  | 404, 410 |
| `HEAD`   | `/{code}`                 | Same as GET without recording a click        | 307     | 404, 410 |
| `GET`    | `/healthz`                | Liveness probe                               | 200     | n/a |
| `GET`    | `/readyz`                 | Readiness probe (`SELECT 1`)                 | 200     | 503 |
| `GET`    | `/docs`, `/redoc`, `/openapi.json` | API documentation                   | 200     | n/a |

All `/api/links*` endpoints require `X-API-Key`; the other endpoints are public.
`{code}` accepts `^[A-Za-z0-9_-]{1,32}$`, and anything else gets `404`. A redirect to
an expired link returns `410 {"detail": "Link has expired"}`.

Any endpoint can also return `503 {"detail": "Service temporarily unavailable"}` if
the database fails, or `500 {"detail": "Internal Server Error"}` on an unexpected
error.

## Examples

The management calls read the key from the `API_KEY` environment variable of your
shell.

```bash
# Create a link
curl -i -X POST http://localhost:8000/api/links \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/a?b=1"}'
# HTTP/1.1 201 Created
# location: /api/links/aB3xY9Q
# {"code":"aB3xY9Q","short_url":"http://localhost:8000/aB3xY9Q",
#  "url":"https://example.com/a?b=1","is_active":true,
#  "created_at":"2025-01-15T10:30:00.123456Z","expires_at":null,"is_expired":false}

# Create a link with a custom alias that expires at the start of 2030 (UTC)
curl -i -X POST http://localhost:8000/api/links \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/launch", "alias": "launch-2030", "expires_at": "2030-01-01T00:00:00Z"}'
# HTTP/1.1 201 Created
# location: /api/links/launch-2030
# Same alias again: 409 {"detail":"Alias already in use"}
# Reserved alias such as "docs": 422 {"detail":"Alias is reserved"}

# Create idempotently (safe to retry; a replay adds "Idempotent-Replayed: true")
curl -i -X POST http://localhost:8000/api/links \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 6f1c2d0e-order-42' \
  -d '{"url": "https://example.com/a?b=1"}'

# Follow the short link (do not follow the redirect, to see the 307)
curl -i http://localhost:8000/aB3xY9Q
# HTTP/1.1 307 Temporary Redirect
# location: https://example.com/a?b=1
# cache-control: no-store

# Look up metadata
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/api/links/aB3xY9Q

# Stats for the last 7 UTC days
curl -s -H "X-API-Key: $API_KEY" 'http://localhost:8000/api/links/aB3xY9Q/stats?days=7'

# Deactivate (soft-delete)
curl -i -X DELETE -H "X-API-Key: $API_KEY" http://localhost:8000/api/links/aB3xY9Q
# HTTP/1.1 204 No Content

# Probes
curl -s http://localhost:8000/healthz   # {"status":"ok"}
curl -s http://localhost:8000/readyz    # {"status":"ready"}
```

## Project layout

The code is split into layers, and dependencies only point inward:
`api → services → repositories → models`.

```
app/
├── main.py                  # create_app(settings=None) factory + module-level `app`
├── api/                     # HTTP layer: routers, dependency providers
│   ├── deps.py              #   get_settings, require_api_key, get_link_service, get_stats_service, build_short_url
│   ├── health.py            #   GET /healthz, GET /readyz
│   ├── links.py             #   /api/links endpoints
│   └── redirect.py          #   GET/HEAD /{code} (registered last: catch-all)
├── services/                # Business rules; own commit/rollback; no FastAPI imports
│   ├── codes.py             #   code generation (secrets), reserved words, code/alias/lookup format checks
│   ├── link_service.py      #   create (alias, expiry, retry + idempotency), lookup, deactivate, redirect + clicks
│   └── stats_service.py     #   stats aggregation
├── repositories/            # SQLAlchemy ORM queries; flush only, never commit
│   ├── link_repository.py
│   ├── click_repository.py
│   └── idempotency_repository.py
├── models/                  # SQLAlchemy 2.0 DeclarativeBase / Mapped models
│   ├── base.py              #   Base, utcnow() (naive UTC)
│   ├── link.py              #   links (unique code, nullable expires_at)
│   ├── click_event.py       #   click_events (index on link_id, created_at; no IP column)
│   └── idempotency_key.py   #   idempotency_keys (unique key)
├── schemas/                 # Pydantic v2 request/response models, URL validation
│   ├── links.py
│   └── health.py
└── core/                    # Cross-cutting concerns
    ├── config.py            #   Settings (env vars)
    ├── db.py                #   engine/session factories, get_db dependency, ensure_schema migration
    ├── errors.py            #   domain exceptions + JSON exception handlers
    ├── logging.py           #   JSON log formatter, configure_logging
    └── middleware.py        #   request logging + last-resort 500 handler
docs/
├── API.md                   # Endpoint reference
└── OPERATIONS.md            # Runbook
```

## Further documentation

- [docs/API.md](docs/API.md): complete endpoint reference
- [docs/OPERATIONS.md](docs/OPERATIONS.md): operations runbook (probes, schema
  migration, logs, failure modes, privacy, scaling, security)
- [CHANGELOG.md](CHANGELOG.md)
