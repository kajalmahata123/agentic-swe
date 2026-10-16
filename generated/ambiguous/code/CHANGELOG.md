# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Campaign readiness. Planned to ship as **1.2.0**. On release, rename this heading to
`## [1.2.0] - YYYY-MM-DD` and add a new empty `[Unreleased]` section. Upgrade notes:
back up the database before deploying. The first start adds the `links.expires_at`
column automatically and switches file-based SQLite to WAL. Run exactly one uvicorn
worker on SQLite. See `docs/OPERATIONS.md` ("Startup and the automatic schema
upgrade", "Campaign deployment").

### Added

- Link expiry: `POST /api/links` accepts an optional `expires_at` (ISO 8601 string or
  `null`). Offsets are converted to UTC, values without an offset are treated as UTC,
  and the value must be in the future (`422 {"detail": "expires_at must be in the
  future"}`). Create and detail responses return `expires_at` (UTC, `Z`, or `null`),
  and `GET /api/links/{code}` also returns `is_expired`.
- New nullable column `links.expires_at` (naive UTC; `NULL` = never expires). An
  idempotent startup schema upgrade (`app/core/schema.py`) adds it to existing
  databases after `create_all`, logs `schema upgrade: links.expires_at added` or
  `schema upgrade: links.expires_at already present`, and aborts startup with
  `schema upgrade failed: could not add links.expires_at` on failure. The equivalent
  manual SQL is documented in `docs/OPERATIONS.md`.
- `PATCH /api/links/{code}` sets, changes or clears (`{"expires_at": null}`) a link's
  expiry. It returns the updated detail (200), 401, 404 or 422, only changes
  `expires_at` and never reactivates a deactivated link.
- `GET /api/links/{code}/stats/hourly` returns zero-filled clicks per UTC hour over
  `[start, end)`. `start` is floored and `end` rounded up to the hour, both or neither
  must be given (default: the last 24 hours plus the current hour, 25 buckets), and
  ranges are capped at 744 hours.
- `HEAD /healthz` and `HEAD /readyz` for uptime monitors (200 with an empty body, or
  503 for readiness). They are not listed in the OpenAPI schema.
- Settings `SQLITE_BUSY_TIMEOUT_MS` (default 5000, 0–60000), `DB_POOL_SIZE` (default
  5, ≥ 1), `DB_MAX_OVERFLOW` (default 10, ≥ 0) and `DB_POOL_TIMEOUT_SECONDS` (default
  30, > 0 and ≤ 300).
- A startup log line `database tuning ...` with the effective dialect, pool settings
  and, for SQLite, `journal_mode`, `synchronous` and `busy_timeout_ms` (never the URL).
- Documentation: a "Campaign deployment" runbook (targets, topology, recommended
  settings, setting and verifying campaign expiry, hourly clicks per TV slot,
  pre-launch checklist, restart and recovery) and a "Scaling beyond the campaign
  profile" section (PostgreSQL migration path) in `docs/OPERATIONS.md`.
- Documentation: MySQL/MariaDB collation note with the DDL for existing databases:
  `ALTER TABLE links MODIFY code VARCHAR(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL;`.

### Changed

- File-based SQLite now runs in WAL journal mode with `synchronous=NORMAL`, the busy
  timeout from `SQLITE_BUSY_TIMEOUT_MS` and a `QueuePool` sized by the `DB_POOL_*`
  settings. Redirects (readers) are no longer blocked by writes. The database now has
  `-wal` and `-shm` sidecar files; back up with `sqlite3 .backup` or copy all three
  files while the service is stopped. Server databases also use the pool settings.
  In-memory SQLite (`sqlite://`, tests only) ignores the pool settings and uses a
  `StaticPool`, so every request thread sees the same in-memory database.
- `GET /{code}` and `HEAD /{code}` return `410 {"detail": "Link has expired"}` once
  `expires_at` is reached. Deactivation takes precedence
  (`410 Link has been deactivated`). `404` and `410` on the redirect path now carry
  `Cache-Control: no-store`.
- The redirect does one narrow lookup (`id`, `url`, `is_active`, `expires_at`) and
  ends the read transaction before recording the click.
- Daily stats (`GET /api/links/{code}/stats`) are aggregated in SQL with `GROUP BY`
  per UTC day instead of loading every click timestamp. The response contract is
  unchanged.
- A failed click write now logs one WARNING
  `failed to record click for link_id=<id> (<ExceptionClass>)` without traceback,
  exception message or IP (previously an ERROR with traceback). The redirect still
  returns 307.
- Idempotency: the request hash covers `expires_at` only when it is present, so keys
  stored by earlier releases still replay; reusing a key with an added, changed or
  removed `expires_at` returns 409.
- On MySQL/MariaDB, fresh databases create `links.code` with the binary collation
  `utf8mb4_bin`, so short codes match case-sensitively. SQLite and PostgreSQL DDL is
  unchanged.

### Fixed

- On SQLite, daily and hourly stats bucket clicks stored with sub-millisecond
  precision exactly (e.g. a click at `23:59:59.999999` counts for that day and hour).
  The stored timestamp is cut to whole seconds before SQLite's date functions, which
  round fractional seconds and on some versions return the wrong day.
- With an in-memory SQLite URL (`sqlite://`), link requests no longer fail with 503
  "no such table": the tables created at startup are now visible to every request.
- `HEAD` probes on `/healthz` and `/readyz` no longer fall through to the catch-all
  `HEAD /{code}` route (which answered 404).
- The SQLite locking runbook in `docs/OPERATIONS.md` is corrected: under WAL, readers
  are not blocked; click writes wait up to `SQLITE_BUSY_TIMEOUT_MS` and are then
  dropped with a WARNING while the redirect still returns 307; creates may return 503
  after the timeout; a failing lookup makes the redirect return 503.
- The README configuration table now lists every setting, including `API_KEY` and the
  new database settings.

### Security

- API-key authentication on all `/api/links*` endpoints is now fully documented in
  `README.md`, `docs/API.md` and `docs/OPERATIONS.md`: the `X-API-Key` header must
  match `API_KEY` (constant-time comparison); missing or wrong keys get
  `401 {"detail": "Invalid or missing API key"}` with `WWW-Authenticate: ApiKey`; the
  API fails closed when `API_KEY` is unset; `API_KEY` must be at least 16 characters.
  Redirects and probes stay public. The new PATCH and hourly stats endpoints are
  covered by the same key.
- Corrected the security notes published with 1.0.0, which described access control
  for the management endpoints inaccurately (see the 1.0.0 Security section below).

## [1.0.0]

First release of the URL Shortener HTTP service.

### Added

- `POST /api/links` creates short links for absolute `http`/`https` URLs. Each
  response includes `code`, `short_url`, `url`, `is_active` and `created_at`, plus a
  `Location: /api/links/{code}` header.
- Strict URL validation: at most 2048 characters, `http`/`https` scheme only
  (case-insensitive), host required, and no whitespace, control or non-ASCII
  characters. URLs are stored unchanged.
- Random 7-character base62 short codes from `secrets`. The length is configurable
  with `SHORT_CODE_LENGTH`. Codes are backed by a database unique constraint, retried
  on collision up to `CODE_MAX_ATTEMPTS` times (default 5, then 503), and never
  match a reserved path segment.
- Idempotent creation with the `Idempotency-Key` header. A replay returns the
  original 201 body with `Idempotent-Replayed: true`, reusing a key with a different
  URL returns 409, and an invalid key returns 400. A database unique constraint
  enforces this.
- `GET /{code}` redirects with 307, an exact `Location` header and
  `Cache-Control: no-store`. It returns 404 for unknown codes and 410 for
  deactivated links. `HEAD /{code}` is supported and does not record a click.
- `GET /api/links/{code}` returns link metadata, including `deactivated_at` and
  `total_clicks`.
- `DELETE /api/links/{code}` soft-deletes a link (204). It is idempotent and keeps
  the click history.
- Click events record the UTC timestamp, the referrer (truncated to 2048 characters)
  and the user agent (truncated to 512 characters). Raw IPs are never stored. The
  optional `ip_hash` holds an HMAC-SHA256 of the client IP when `IP_HASH_SALT` is
  set. Click recording is best-effort and never breaks a redirect.
- `GET /api/links/{code}/stats` returns all-time `total_clicks`, `clicks_per_day`
  for the UTC-day window set by `days` (1–365, default 30) and the top 10
  referrers.
- `GET /healthz` liveness probe and `GET /readyz` readiness probe (`SELECT 1`, 503
  on failure).
- Consistent JSON `{"detail": ...}` errors for 400, 404, 405, 409, 410, 422 and 503.
  Unhandled errors return a generic `500 {"detail": "Internal Server Error"}`, and the
  traceback is logged server-side only.
- Structured JSON request logging (method, path, status, duration_ms) that never
  includes client IPs. The level is set with `LOG_LEVEL`.
- Configuration through the environment variables `DATABASE_URL`, `BASE_URL`,
  `SHORT_CODE_LENGTH`, `CODE_MAX_ATTEMPTS`, `IP_HASH_SALT` and `LOG_LEVEL`. Tables
  are created automatically at startup. SQLite is the default, and PostgreSQL/MySQL
  work through `DATABASE_URL`.
- OpenAPI documentation at `/docs`, `/redoc` and `/openapi.json`.
- Documentation: `README.md`, `docs/API.md` and `docs/OPERATIONS.md`.

### Security

- The management endpoints under `/api/links*` are protected by API-key
  authentication (`X-API-Key` header matching the `API_KEY` setting, at least 16
  characters; 401 otherwise, and fail closed when `API_KEY` is unset). The redirect
  and the probes are public. (Corrected entry: the note originally published here
  misdescribed access control.)
- Rate limiting is not built in; configure it at the gateway before a public launch.
- Run uvicorn with `--no-access-log` so that client IPs are not logged.
