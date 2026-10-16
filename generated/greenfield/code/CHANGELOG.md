# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

- No authentication on the management endpoints in this release; this is a known
  risk. Restrict access at the gateway before a public launch.
- Run uvicorn with `--no-access-log` so that client IPs are not logged.
