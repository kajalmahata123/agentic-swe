# URL Shortener

An HTTP service for creating short links to long `http`/`https` URLs. Visiting a
short link redirects to the original URL with **HTTP 307**. Every redirect is
recorded as a click event that **never stores the raw client IP**, and each link has
its own stats (total clicks, clicks per UTC day, clicks per UTC hour, top referrers).
Links can be given an expiry instant, after which they stop redirecting.

Built with Python 3.11, FastAPI, SQLAlchemy 2.0 (synchronous ORM) and Pydantic v2.
It uses SQLite by default (hardened with WAL) and runs on PostgreSQL or MySQL if you
change `DATABASE_URL`.

> **Security notice: API-key authentication.** Every management endpoint under
> `/api/links*` requires the `X-API-Key` header to match the `API_KEY` setting
> (compared in constant time). A missing or wrong key returns
> `401 {"detail": "Invalid or missing API key"}` with `WWW-Authenticate: ApiKey`.
> The API **fails closed**: if `API_KEY` is unset (or blank), every `/api/links*`
> request is rejected with 401 and a WARNING is logged at startup. `API_KEY` must be
> at least **16 characters**; a shorter value stops the application at startup.
> The public redirect (`GET`/`HEAD /{code}`) and the probes (`/healthz`, `/readyz`)
> need no key. Rate limiting is not built in; configure it at your gateway. See
> [docs/OPERATIONS.md](docs/OPERATIONS.md#security-notes).

## Features

- **Create short links:** `POST /api/links` with `{"url": "..."}`. The URL is stored
  exactly as submitted.
- **Strict URL validation:** only absolute `http`/`https` URLs with a host are
  accepted, up to 2048 characters. URLs with whitespace, control or non-ASCII
  characters are rejected, as are `javascript:`, `data:`, `file:`, `ftp:` and
  `mailto:` URLs and relative URLs. To use an internationalised URL, send it in
  punycode or percent-encoded form.
- **Random, collision-safe codes:** 7 base62 characters (`[A-Za-z0-9]`) from Python's
  `secrets` CSPRNG. A database unique constraint backs the codes. On a collision the
  service retries, up to 5 attempts by default. Codes that match reserved path segments
  (`healthz`, `readyz`, `api`, `docs`, `redoc`, …) are never issued. Codes are
  case-sensitive (`/AbC1234` and `/abc1234` are different links).
- **Idempotent creation:** send an `Idempotency-Key` header and a retried request
  returns the original link with `Idempotent-Replayed: true` instead of creating a
  duplicate. A database unique constraint enforces this, so it also works across
  concurrent requests and multiple instances.
- **Link expiry:** set an optional `expires_at` (ISO 8601, stored as UTC) when
  creating a link, and set, change or clear it later with
  `PATCH /api/links/{code}`. From `expires_at` onwards the redirect returns
  `410 {"detail": "Link has expired"}`. Metadata shows `expires_at` and `is_expired`.
- **307 redirects** (`GET /{code}`) with `Cache-Control: no-store`, so every visit is
  counted and deactivation or expiry takes effect immediately. `404`/`410` responses
  on this path are also `no-store`. `HEAD /{code}` is supported and does not count as
  a click.
- **Soft delete:** `DELETE /api/links/{code}` deactivates a link and keeps its row
  and click history. Redirects to a deactivated link return
  `410 {"detail": "Link has been deactivated"}`.
- **Privacy-preserving click analytics:** each click stores the timestamp, `Referer`
  and `User-Agent`. The raw IP is never stored. If `IP_HASH_SALT` is set, the service
  stores an HMAC-SHA256 hash of the IP instead.
- **Stats:** all-time total clicks, clicks per UTC day for a configurable window
  (default 30 days) and the top 10 referrers, aggregated in SQL.
- **Hourly stats:** `GET /api/links/{code}/stats/hourly` returns zero-filled clicks
  per UTC hour over a `[start, end)` range of up to 744 hours (31 days), for example
  to measure the response to a TV ad slot.
- **Probes:** `/healthz` (liveness, does not use the DB) and `/readyz` (readiness,
  runs `SELECT 1`). Both answer `GET` (JSON body) and `HEAD` (empty body), so uptime
  monitors that probe with `HEAD` work.
- **Hardened SQLite:** file-based SQLite runs in WAL journal mode with
  `synchronous=NORMAL`, so redirects (readers) are not blocked by writes. The busy
  timeout and connection pool are configurable.
- **Automatic schema upgrade:** at startup the service adds the `links.expires_at`
  column to databases created by earlier releases.
- **Consistent JSON errors:** every error body has the form `{"detail": ...}`. Stack
  traces and exception messages are never returned to clients.
- **Structured JSON logs:** one line per request with method, path, status and
  duration, plus startup lines for the database tuning and the schema upgrade. Logs
  never contain client IPs, query strings or the API key.
- **OpenAPI docs** at `/docs`, `/redoc` and `/openapi.json` (security scheme
  `ApiKeyAuth`).

## Quick start

Requires Python 3.11 or later.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# The management API needs a key of at least 16 characters.
export API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

# Run the service. --no-access-log is required: uvicorn's access log prints client IPs.
# Use exactly one worker with SQLite.
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --no-access-log
```

When the service starts, it creates any missing tables (`links`, `click_events`,
`idempotency_keys`) in the configured database, adds the `links.expires_at` column if
it is missing, and logs one `database tuning ...` line. By default the database is
the file `./shortener.db` (plus the WAL files `./shortener.db-wal` and
`./shortener.db-shm`).

Open http://localhost:8000/docs for the interactive API documentation. Use the
**Authorize** button to enter your API key.

### Running the tests

```bash
python -m pytest -q
python -m pytest -q --cov=app --cov-report=term-missing   # with coverage
python -m pytest -q -m "not slow"                          # skip the slow performance/load tests
```

Each test runs against its own temporary SQLite database, because the app factory
`create_app(Settings(database_url=...))` creates a separate engine per app instance.
The tests don't need any external services.

## Configuration

All settings come from environment variables. Names are case-insensitive, and no
`.env` file is read. If a value is invalid (for example `SHORT_CODE_LENGTH=2`,
`LOG_LEVEL=verbose`, `API_KEY=short` or `DB_POOL_SIZE=0`), the application fails at
startup with a validation error.

| Variable                  | Default                    | Allowed values | Description |
|---------------------------|----------------------------|----------------|-------------|
| `DATABASE_URL`            | `sqlite:///./shortener.db` | SQLAlchemy URL | Database URL. For SQLite, `check_same_thread=False` is applied automatically; file-based SQLite also gets WAL, `synchronous=NORMAL`, the busy timeout and a `QueuePool`. Other backends get `pool_pre_ping=True` and the pool settings. PostgreSQL and MySQL also need a driver that is **not** in `requirements.txt` (no driver ships in this release), e.g. `psycopg[binary]` for `postgresql+psycopg://...` or `pymysql` for `mysql+pymysql://...`. |
| `BASE_URL`                | `http://localhost:8000`    | URL            | Public base URL used to build `short_url`. A trailing `/` is stripped. |
| `SHORT_CODE_LENGTH`       | `7`                        | integer, 4–32  | Length of newly generated codes. |
| `CODE_MAX_ATTEMPTS`       | `5`                        | integer, 1–20  | Number of code-generation attempts before `POST /api/links` returns 503. |
| `IP_HASH_SALT`            | *(unset)*                  | any string     | Secret key for hashing client IPs (HMAC-SHA256) into `click_events.ip_hash`. If unset, empty or whitespace-only, `ip_hash` is `NULL`. Raw IPs are never stored either way. |
| `LOG_LEVEL`               | `INFO`                     | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (case-insensitive) | Level of the `app.*` loggers. |
| `API_KEY`                 | *(unset)*                  | string, at least 16 characters | Shared secret that clients send in the `X-API-Key` header on every `/api/links*` request. Empty or whitespace-only counts as unset. **Unset means every `/api/links*` request gets 401** (fail closed). Inject it from a secret manager. |
| `SQLITE_BUSY_TIMEOUT_MS`  | `5000`                     | integer, 0–60000 | File-based SQLite only: how long a connection waits for the write lock before failing. Recommended for the campaign: `1000`. Ignored for in-memory SQLite and other databases. |
| `DB_POOL_SIZE`            | `5`                        | integer, ≥ 1   | Connections kept open in the pool (per process). Ignored for in-memory SQLite. |
| `DB_MAX_OVERFLOW`         | `10`                       | integer, ≥ 0   | Extra connections the pool may open above `DB_POOL_SIZE` under load. Ignored for in-memory SQLite. |
| `DB_POOL_TIMEOUT_SECONDS` | `30.0`                     | number, > 0 and ≤ 300 | How long a request waits for a free pool connection before failing with 503. Ignored for in-memory SQLite. |

## API overview

Full reference: [docs/API.md](docs/API.md). Every `/api/links*` endpoint requires the
`X-API-Key` header.

| Method   | Path                             | Description                                  | Success | Errors |
|----------|----------------------------------|----------------------------------------------|---------|--------|
| `POST`   | `/api/links`                     | Create a short link (optional `expires_at`, optional `Idempotency-Key`) | 201 | 400, 401, 409, 422, 503 |
| `GET`    | `/api/links/{code}`              | Get link metadata incl. `expires_at`/`is_expired` (also for deactivated and expired links) | 200 | 401, 404 |
| `PATCH`  | `/api/links/{code}`              | Set, change or clear `expires_at` (never reactivates) | 200 | 401, 404, 422 |
| `DELETE` | `/api/links/{code}`              | Deactivate (soft-delete) a link, idempotent  | 204     | 401, 404 |
| `GET`    | `/api/links/{code}/stats`        | Daily click stats (`?days=1..365`, default 30) | 200   | 401, 404, 422 |
| `GET`    | `/api/links/{code}/stats/hourly` | Zero-filled clicks per UTC hour (`?start=&end=`, default last 25 hours, max 744 hours) | 200 | 401, 404, 422 |
| `GET`    | `/{code}`                        | Redirect to the original URL and record a click (public) | 307 | 404, 410 (`Link has been deactivated` / `Link has expired`), 503 |
| `HEAD`   | `/{code}`                        | Same as GET without recording a click (public) | 307   | 404, 410, 503 |
| `GET`    | `/healthz`                       | Liveness probe (public)                      | 200     | n/a |
| `HEAD`   | `/healthz`                       | Liveness probe, empty body (public)          | 200     | n/a |
| `GET`    | `/readyz`                        | Readiness probe, `SELECT 1` (public)         | 200     | 503 |
| `HEAD`   | `/readyz`                        | Readiness probe, empty body (public)         | 200     | 503 |
| `GET`    | `/docs`, `/redoc`, `/openapi.json` | API documentation                          | 200     | n/a |

Any endpoint can also return `503 {"detail": "Service temporarily unavailable"}` if
the database fails, or `500 {"detail": "Internal Server Error"}` on an unexpected
error.

## Examples

```bash
export API_KEY='replace-with-your-key-of-16+chars'

# Create a link
curl -i -X POST http://localhost:8000/api/links \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/a?b=1"}'
# HTTP/1.1 201 Created
# location: /api/links/aB3xY9Q
# {"code":"aB3xY9Q","short_url":"http://localhost:8000/aB3xY9Q",
#  "url":"https://example.com/a?b=1","is_active":true,
#  "created_at":"2025-01-15T10:30:00.123456Z","expires_at":null}

# Create a link that expires (offsets are converted to UTC; returned with "Z")
curl -s -X POST http://localhost:8000/api/links \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/campaign", "expires_at": "2025-12-31T23:59:00Z"}'
# {"code":"Kx7pQ2m",...,"expires_at":"2025-12-31T23:59:00Z"}

# Create idempotently (safe to retry; a replay adds "Idempotent-Replayed: true")
curl -i -X POST http://localhost:8000/api/links \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 6f1c2d0e-order-42' \
  -d '{"url": "https://example.com/a?b=1"}'

# Without a valid key
curl -i -X POST http://localhost:8000/api/links \
  -H 'Content-Type: application/json' -d '{"url": "https://example.com/"}'
# HTTP/1.1 401 Unauthorized
# www-authenticate: ApiKey
# {"detail":"Invalid or missing API key"}

# Follow the short link (do not follow the redirect, to see the 307)
curl -i http://localhost:8000/aB3xY9Q
# HTTP/1.1 307 Temporary Redirect
# location: https://example.com/a?b=1
# cache-control: no-store

# After expires_at has passed
curl -i http://localhost:8000/Kx7pQ2m
# HTTP/1.1 410 Gone
# cache-control: no-store
# {"detail":"Link has expired"}

# Look up metadata (includes expires_at and is_expired)
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/api/links/aB3xY9Q

# Set, change or clear the expiry of an existing link
curl -s -X PATCH http://localhost:8000/api/links/aB3xY9Q \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"expires_at": "2025-12-31T23:59:00Z"}'
curl -s -X PATCH http://localhost:8000/api/links/aB3xY9Q \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"expires_at": null}'

# Stats for the last 7 UTC days
curl -s -H "X-API-Key: $API_KEY" 'http://localhost:8000/api/links/aB3xY9Q/stats?days=7'

# Clicks per UTC hour (default: the last 24 hours plus the current hour)
curl -s -H "X-API-Key: $API_KEY" 'http://localhost:8000/api/links/aB3xY9Q/stats/hourly'
# An explicit range; '+' in an offset must be sent as %2B (or let curl encode it)
curl -s -G -H "X-API-Key: $API_KEY" http://localhost:8000/api/links/aB3xY9Q/stats/hourly \
  --data-urlencode 'start=2025-11-20T20:00:00+01:00' \
  --data-urlencode 'end=2025-11-20T23:00:00+01:00'

# Deactivate (soft-delete)
curl -i -X DELETE -H "X-API-Key: $API_KEY" http://localhost:8000/api/links/aB3xY9Q
# HTTP/1.1 204 No Content

# Probes (public)
curl -s http://localhost:8000/healthz   # {"status":"ok"}
curl -s http://localhost:8000/readyz    # {"status":"ready"}
curl -I http://localhost:8000/readyz    # HTTP/1.1 200 OK, empty body
```

## Project layout

The code is split into layers, and dependencies only point inward:
`api → services → repositories → models`.

```
app/
├── main.py                  # create_app(settings=None) factory + module-level `app`
├── api/                     # HTTP layer: routers, dependency providers
│   ├── deps.py              #   get_settings, require_api_key, get_link_service, get_stats_service, build_short_url
│   ├── health.py            #   GET/HEAD /healthz, GET/HEAD /readyz
│   ├── links.py             #   /api/links endpoints (X-API-Key required)
│   └── redirect.py          #   GET/HEAD /{code} (registered last: catch-all)
├── services/                # Business rules; own commit/rollback; no FastAPI imports
│   ├── codes.py             #   code generation (secrets), reserved words, format check
│   ├── link_service.py      #   create (retry + idempotency + expiry), lookup, PATCH expiry, deactivate, redirect + clicks
│   └── stats_service.py     #   daily and hourly stats (SQL aggregation)
├── repositories/            # SQLAlchemy ORM queries; flush only, never commit
│   ├── link_repository.py   #   incl. the narrow redirect lookup
│   ├── click_repository.py  #   incl. daily/hourly GROUP BY queries
│   └── idempotency_repository.py
├── models/                  # SQLAlchemy 2.0 DeclarativeBase / Mapped models
│   ├── base.py              #   Base, utcnow(), to_naive_utc() (naive UTC)
│   ├── link.py              #   links (unique code, expires_at)
│   ├── click_event.py       #   click_events (index on link_id, created_at; no IP column)
│   └── idempotency_key.py   #   idempotency_keys (unique key)
├── schemas/                 # Pydantic v2 request/response models, URL and expiry validation
│   ├── links.py
│   └── health.py
└── core/                    # Cross-cutting concerns
    ├── config.py            #   Settings (env vars)
    ├── db.py                #   engine (WAL, busy timeout, pool), session factory, get_db, tuning log line
    ├── schema.py            #   startup schema upgrade (adds links.expires_at)
    ├── errors.py            #   domain exceptions + JSON exception handlers
    ├── logging.py           #   JSON log formatter, configure_logging
    └── middleware.py        #   request logging + last-resort 500 handler
docs/
├── API.md                   # Endpoint reference
└── OPERATIONS.md            # Runbook, campaign deployment, scaling
```

## Further documentation

- [docs/API.md](docs/API.md): complete endpoint reference
- [docs/OPERATIONS.md](docs/OPERATIONS.md): operations runbook (startup and schema
  upgrade, probes, logs, SQLite WAL and backups, locking, failure modes, privacy,
  campaign deployment, scaling to PostgreSQL)
- [CHANGELOG.md](CHANGELOG.md)
