# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

No changes yet.

## [1.1.0]

CR-142 (custom aliases and link expiration) and the BUG-77 investigation. All changes
are backwards compatible: existing endpoints, request contracts, response fields,
status codes and headers are unchanged, and new response fields are additive. This
release adds the nullable column `links.expires_at`, which an idempotent step adds
automatically at startup (see the schema change under "Changed").

### Added

- CR-142 custom aliases: `POST /api/links` accepts an optional `alias`
  (3–32 characters of `[A-Za-z0-9_-]`, case preserved, stored as the link's `code`,
  immutable). An invalid alias (format, length or a non-string type) returns `422`
  with a list `detail`. A reserved word, checked case-insensitively, returns
  `422 {"detail": "Alias is reserved"}`. An alias that is already taken by any link,
  including deactivated and expired ones, returns `409 {"detail": "Alias already in use"}`.
- CR-142 link expiration: `POST /api/links` accepts an optional `expires_at`
  (an ISO-8601 date-time string; naive values are UTC; it must be in the future,
  otherwise `422 {"detail": "expires_at must be in the future"}`; immutable; no
  maximum). Once expired, `GET/HEAD /{code}` return
  `410 {"detail": "Link has expired"}` with no `Location` and no click recorded.
  `"Link has been deactivated"` still takes precedence. Metadata, stats and `DELETE`
  keep working for expired links, and `is_active` is not changed by expiry.
- New response fields `expires_at` (UTC with `Z`, or `null`) and `is_expired` in
  `LinkCreatedResponse` and `LinkDetailResponse`.
- OpenAPI now documents the `409` and `422` cases of `POST /api/links` and the `410`
  of `GET /{code}`. `HEAD /{code}` is now included in the schema with its
  `307/404/410` responses.

### Changed

- Schema change: new nullable column `links.expires_at` (`DATETIME NULL`, naive UTC,
  `NULL` = never expires). An idempotent migration adds it automatically at startup
  after `create_all`. It logs one INFO line on `app.migrations` when the column is
  added, tolerates a concurrent duplicate-column race, and fails startup on any other
  error. The manual SQL is `ALTER TABLE links ADD COLUMN expires_at DATETIME NULL`
  (PostgreSQL: `TIMESTAMP NULL`). Existing links never expire. Migrate once before a
  multi-instance rollout. Rollback is safe. See `docs/OPERATIONS.md`.
- `{code}` lookups on all code-addressed endpoints accept `^[A-Za-z0-9_-]{1,32}$`
  (previously base62 only), so aliases containing `-` or `_` work. Other values still
  return `404` without a database lookup. Generated codes are still base62.
- `metrics` is now a reserved word, for both generated codes and aliases. The full
  list is `api`, `docs`, `redoc`, `healthz`, `readyz`, `metrics`, `openapi.json`,
  `static`, `favicon.ico`.
- The idempotency `request_hash` now covers `alias` and `expires_at`, so reusing a
  key with a different alias or expiry returns `409`. This is backwards compatible:
  requests without either field still hash as `sha256(url)`, so keys stored by 1.0.0
  keep replaying. Otherwise the hash is the SHA-256 of canonical JSON
  `{"alias","expires_at","url"}` (sorted keys, `expires_at` normalised to UTC). Replays reflect the link's current state, including `is_expired`.
- When two requests race to insert the same alias, the loser gets `409 Alias already
  in use`. It never gets a random code or a `503`.

### Fixed

- The documentation (`README.md`, `docs/API.md` and `docs/OPERATIONS.md`) wrongly
  stated that there was no authentication. In fact,
  every `/api/links*` endpoint requires an `X-API-Key` header that matches the
  `API_KEY` setting. The check fails closed (no `API_KEY` configured means every
  request is rejected), the key is compared in constant time, and a missing or wrong
  key returns `401 {"detail": "Invalid or missing API key"}`. `GET/HEAD /{code}` and
  the probes are public.

### Investigated

- BUG-77 (URL scheme validation): could not be reproduced in `validate_target_url`. Upper-case schemes such as
  `HTTP://Example.com` and `HTTPS://EXAMPLE.COM/Path` are accepted and stored and
  redirected unchanged. Every `javascript:` variant is rejected with `422` and
  nothing is stored, including mixed case, leading whitespace or tab, an embedded
  newline, `javascript://...` and `http:javascript:...`. The likely causes of the
  report are:
  - a `401` caused by a missing `X-API-Key`, mistaken for a validation failure;
  - surrounding whitespace or newlines in the submitted URL, which are rejected by
    design;
  - rows inserted directly into the database, bypassing validation.

  Regression tests were added, also covering requests that send `alias` and
  `expires_at`. There is no behaviour change.

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
