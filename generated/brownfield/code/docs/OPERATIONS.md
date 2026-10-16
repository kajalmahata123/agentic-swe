# Operations Runbook

URL Shortener 1.1.0. This release adds CR-142 (custom aliases, link expiration and
the `links.expires_at` schema change) on top of 1.0.0. This runbook covers how to run,
monitor and troubleshoot the service in production. **When you upgrade from 1.0.0,
read [Schema migration](#schema-migration-linksexpires_at) first.**

## Contents

- [Running the service](#running-the-service)
- [Schema migration: links.expires_at](#schema-migration-linksexpires_at)
- [Health checks](#health-checks)
- [Logs](#logs)
- [Metrics](#metrics)
- [Failure modes and responses](#failure-modes-and-responses)
- [Data model, retention and privacy](#data-model-retention-and-privacy)
- [Scaling](#scaling)
- [Security notes](#security-notes)

---

## Running the service

```bash
pip install -r requirements.txt
export API_KEY='<a secret of at least 16 characters, from your secret manager>'
uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log
```

Without `API_KEY`, the service still starts and serves redirects and probes, but every
`/api/links*` request gets `401` (fail closed). See [Security notes](#security-notes).

**Always pass `--no-access-log`.** uvicorn's built-in access log prints the client IP
on every request, which breaks the service's privacy guarantee (raw IPs are never
logged or stored). The app writes its own request log without IPs (see [Logs](#logs)).

Startup:

1. `Settings` are read from environment variables (see the configuration table in
   [README.md](../README.md#configuration)). Invalid values such as
   `SHORT_CODE_LENGTH=2` or `LOG_LEVEL=verbose` stop the process with a validation
   error. So does an `API_KEY` shorter than 16 characters.
2. The database engine is created. Importing `app.main` doesn't connect to the
   database.
3. During the ASGI lifespan startup, `Base.metadata.create_all` creates any missing
   tables and indexes. `create_all` never alters existing tables.
4. Next, the automatic schema migration (`ensure_schema` in `app/core/db.py`) adds
   the `links.expires_at` column if it is missing. It is idempotent: once the column
   exists, it does nothing. See
   [Schema migration](#schema-migration-linksexpires_at). There is no other migration
   tooling (no Alembic).
5. On shutdown, the engine's connection pool is disposed.

Container probes: use `GET /healthz` for liveness and `GET /readyz` for readiness.

---

## Schema migration: links.expires_at

Release 1.1.0 (CR-142) adds one nullable column to the `links` table:

| Column | Type | Meaning |
|--------|------|---------|
| `links.expires_at` | `DATETIME NULL` (`TIMESTAMP NULL` on PostgreSQL) | Naive UTC expiry time. `NULL` means the link never expires. |

Existing rows get `NULL`, so every existing link keeps working and never expires. No
other table changes. `links.code` is already `VARCHAR(32)` with the unique constraint
`uq_links_code`, which also holds aliases, and `idempotency_keys` keeps its columns.

### Automatic migration at startup

On every start, after `create_all`:

| Situation | What happens | Log (`app.migrations` logger) |
|-----------|--------------|-------------------------------|
| Fresh database | `create_all` already created the column, so there is nothing to do. | DEBUG only |
| Column already present (second and later starts) | Nothing to do. | DEBUG `schema up to date` |
| Column missing (database created by 1.0.0, before CR-142) | Runs `ALTER TABLE links ADD COLUMN expires_at DATETIME NULL` (on PostgreSQL: `... TIMESTAMP NULL`). | One INFO line: `schema migration applied: added column links.expires_at` |
| The `ALTER` fails because another instance added the column at the same moment (duplicate-column error) | Tolerated: the column now exists, so startup continues. | INFO `links.expires_at already added by another instance` |
| The `ALTER` fails for any other reason (e.g. missing `ALTER` privilege, locked database) | The error is **not** swallowed. Startup fails and the process doesn't serve traffic. | ERROR `schema migration failed: could not add links.expires_at`, then the database exception that uvicorn reports |

The database user needs `ALTER TABLE` rights on `links` for the one start that adds
the column. If you don't want to grant that, apply the SQL manually first.

### Manual migration (for DBAs)

You can apply the change yourself before deploying. After that, the automatic step
finds the column and does nothing.

SQLite and MySQL:

```sql
ALTER TABLE links ADD COLUMN expires_at DATETIME NULL;
```

PostgreSQL (it has no `DATETIME` type; `TIMESTAMP` is what SQLAlchemy's `DateTime`
creates there anyway):

```sql
ALTER TABLE links ADD COLUMN expires_at TIMESTAMP NULL;
```

Check the result, for example with `PRAGMA table_info(links);` (SQLite),
`SHOW COLUMNS FROM links;` (MySQL) or `\d links` (PostgreSQL).

### Rollout and rollback

- **Multi-instance deployments: migrate once before rolling out.** Either run the
  manual SQL, or start a single new instance and wait until it is ready, and only then
  roll out the remaining instances. The race between instances is tolerated, but
  migrating once keeps `ALTER TABLE` (and the table lock it may take) off the
  rolling-deploy path.
- During a rolling deploy, old instances keep working against the migrated schema:
  they don't know the column and create links with `expires_at = NULL`.
- **Rollback to 1.0.0 is safe.** You don't need to drop the column, because 1.0.0
  ignores it. While 1.0.0 runs, though, it doesn't enforce expiry (expired
  links redirect again). It also returns `404` for aliases containing `-` or `_`,
  because it accepts only base62 codes. Idempotency keys first used with `alias` or
  `expires_at` will get `409` instead of replaying. All of this comes back once you
  roll forward again.
- Back up the database before the first start of 1.1.0, as for any schema change.

---

## Health checks

| Endpoint | Purpose | Touches DB | Healthy | Unhealthy |
|----------|---------|-----------|---------|-----------|
| `GET /healthz` | Liveness: the process is serving HTTP | No | `200 {"status":"ok"}` | No response or timeout |
| `GET /readyz` | Readiness: the database accepts queries (`SELECT 1`) | Yes | `200 {"status":"ready"}` | `503 {"detail":"Database is not ready"}` (or `"Service temporarily unavailable"`) |

Example Kubernetes configuration:

```yaml
livenessProbe:
  httpGet: {path: /healthz, port: 8000}
  periodSeconds: 10
  failureThreshold: 3
readinessProbe:
  httpGet: {path: /readyz, port: 8000}
  periodSeconds: 5
  failureThreshold: 2
```

Don't use `/readyz` as the liveness probe. A database outage would then restart every
pod without fixing anything.

---

## Logs

The `app` logger hierarchy writes **one JSON object per line to stderr**. `LOG_LEVEL`
sets the level, and the default is `INFO`.

Common keys: `ts` (ISO 8601 UTC), `level`, `logger`, `message`. Request lines also
have `method`, `path`, `status` and `duration_ms`. Error lines include the formatted
traceback under `exc`.

### Log events

| Logger | Level | `message` | Meaning |
|--------|-------|-----------|---------|
| `app.request` | INFO | `request` | One line per HTTP request, with `method`, `path` (no query string), `status` and `duration_ms`. |
| `app.errors` | ERROR | `unhandled exception` | An unexpected exception. The client got `500 {"detail":"Internal Server Error"}`. The traceback is in `exc`. |
| `app.errors` | ERROR | `database error` | A `SQLAlchemyError` reached the error handler. The client got `503 {"detail":"Service temporarily unavailable"}`. The traceback is in `exc`. |
| `app.health` | WARNING | `readiness check failed` | `/readyz` returned 503. |
| `app.services.links` | ERROR | `failed to record click for link_id=<id>` | A click event could not be written. The redirect still succeeded. The traceback is in `exc`. |
| `app.services.links` | ERROR | `short code allocation exhausted` | Every code-generation attempt collided or was reserved. The client got 503. |
| `app.migrations` | INFO | `schema migration applied: added column links.expires_at` | Startup added the column to a 1.0.0 (pre-CR-142) database. Logged once per database. |
| `app.migrations` | INFO | `links.expires_at already added by another instance` | A concurrent instance won the migration race. Startup continued. |
| `app.migrations` | ERROR | `schema migration failed: could not add links.expires_at` | The migration failed and startup was aborted. See [Startup fails](#startup-fails). |
| `app.migrations` | DEBUG | `schema up to date` | Normal start with nothing to migrate. Visible only with `LOG_LEVEL=DEBUG`. |

Alias and `expires_at` values are never logged.

Sample lines:

```json
{"ts": "2025-01-15T10:30:00.123456+00:00", "level": "INFO", "logger": "app.request", "message": "request", "method": "GET", "path": "/aB3xY9Q", "status": 307, "duration_ms": 3.41}
{"ts": "2025-01-15T10:31:12.004211+00:00", "level": "ERROR", "logger": "app.services.links", "message": "failed to record click for link_id=17", "exc": "Traceback (most recent call last): ..."}
```

### What is never logged

The app's logs never contain client IPs, `X-Forwarded-For`, request headers, query
strings or request bodies. Exception details go only to the server-side log, never to
HTTP responses. Logs from uvicorn itself (`uvicorn.error`, and the access log if
enabled) are separate and do not use the JSON format.

---

## Metrics

Version 1.1.0 has **no metrics endpoint** (no Prometheus `/metrics`). You can derive
these signals from the `app.request` log lines with your log pipeline:

| Signal | Source | Suggested alert |
|--------|--------|-----------------|
| Request rate per route | count of `app.request` grouped by `method` and `path` prefix (`/api/links`, `/{code}`) | n/a |
| Redirect latency p95 | `duration_ms` where `status = 307` | p95 > 50 ms sustained (NFR-4 target on SQLite) |
| Create latency p95 | `duration_ms` where `method = POST` and `path = /api/links` | p95 > 100 ms sustained |
| 5xx rate | `status >= 500` | any sustained 500s; 503 rate > 1 % |
| Lost clicks | count of `failed to record click` | > 0 sustained |
| Code exhaustion | count of `short code allocation exhausted` | any |
| Readiness | `/readyz` probe result / `readiness check failed` | failing for more than 1 minute |

You can query the database directly for business metrics, such as links created per
day (`links.created_at`), clicks per day (`click_events.created_at`) and expired
links (`links.expires_at <= <current UTC time>`).

`metrics` is a reserved word, so neither a generated code nor an alias can ever take
the path `/metrics`. That leaves room to add a metrics endpoint later.

---

## Failure modes and responses

### Database unreachable or erroring

- **Symptoms:** `/readyz` returns 503. API calls and redirects return
  `503 {"detail":"Service temporarily unavailable"}`. ERROR `database error` lines
  appear in the log.
- **Response:** check database availability, credentials in `DATABASE_URL`, network
  access and the connection limit. On non-SQLite backends, `pool_pre_ping=True` means
  the pool recovers from dropped connections by itself once the database is back. No
  restart is needed.

### SQLite "database is locked"

- **Cause:** concurrent writes (link creation plus click recording) against one SQLite
  file, usually with several uvicorn workers or processes.
- **Symptoms:** sporadic 503s on `POST /api/links` and `DELETE`, and
  `failed to record click` errors. Redirects themselves still return 307.
- **Response:** run a single process on SQLite, or move to PostgreSQL/MySQL (see
  [Scaling](#scaling)). SQLite is meant for development and small single-instance
  deployments.

### Clicks not being recorded

- **Symptoms:** redirects work but `total_clicks` stops increasing, and the log shows
  `failed to record click for link_id=...`.
- **Explanation:** click recording is best-effort by design. The redirect is never
  failed because of analytics. Look at the traceback in `exc` (common causes are a
  locked database, a full disk, or a missing table after manual schema changes).
- **Response:** fix the underlying database problem. Clicks lost during the incident
  can't be recovered.

### 503 "Could not allocate a unique short code, please retry"

- **Cause:** all `CODE_MAX_ATTEMPTS` candidate codes collided with existing codes or
  were reserved words. With 7-character base62 codes this is practically impossible
  unless the code space is nearly full or `SHORT_CODE_LENGTH` is very small.
- **Response:** check `SHORT_CODE_LENGTH` (raise it if it was set low) and
  `CODE_MAX_ATTEMPTS`. Changing the length affects only new codes. Existing codes of
  any length up to 32 keep working.

### 500 "Internal Server Error"

- **Cause:** a bug or another unexpected exception.
- **Response:** find the `unhandled exception` line from `app.errors` at the same
  time; it has the full traceback in `exc`. The client only ever sees the generic
  body.

### 401 "Invalid or missing API key" on /api/links*

- **Cause:** the client sent no `X-API-Key` header or a wrong one, or `API_KEY` isn't
  set on this instance (fail closed). The key is checked before the request body, so
  a client without a valid key sees `401` even for an invalid body. Redirects and
  probes are not affected.
- **Response:** check that `API_KEY` is set (at least 16 characters) on every
  instance and matches the key the client uses. When you rotate the key, update
  clients and all instances together, because only one key is accepted at a time.

### 409 responses on POST

- `"Alias already in use"`: the requested alias is taken by an existing link. This
  includes deactivated and expired links, because aliases are never released. It is
  also what the loser of a race for the same alias gets. This is expected; the client
  has to pick another alias.
- `"Idempotency-Key already used with a different payload"`: a client reused a key
  with a different `url`, `alias` or `expires_at`. If clients report it unexpectedly,
  they are probably generating keys that aren't unique. Keys are global and never
  expire.
- `"Idempotency-Key conflict, please retry"` is a rare race outcome. Retrying the
  request is safe.

### 410 "Link has expired"

- Expected once a link's `expires_at` has passed. The link is still `is_active: true`
  and its metadata and stats remain available. `expires_at` can't be changed through
  the API. If a link must be extended, create a new one.
- Expiry is evaluated against the server clock in UTC. Keep server clocks
  synchronised (NTP), especially when several instances run.

### Startup fails

- The process exits with a settings validation error: fix the offending environment
  variable (for example an `API_KEY` shorter than 16 characters).
- `create_all` fails because of database errors or permissions: the database user
  needs `CREATE TABLE` / `CREATE INDEX` rights on first start. With a SQLite file
  path, the directory must be writable.
- The log shows `schema migration failed: could not add links.expires_at`: the
  automatic migration couldn't add the column. Common causes are a missing `ALTER`
  privilege or a locked SQLite file. Fix the cause, or apply the
  [manual SQL](#manual-migration-for-dbas), and then restart.
- `ModuleNotFoundError` for a database driver: install the driver that matches
  `DATABASE_URL` (e.g. `psycopg[binary]`, `pymysql`). Only SQLite works out of the
  box.

### Abuse / phishing takedown

Deactivate the link with `DELETE /api/links/{code}`. It takes effect immediately:
redirects return 410 and nothing is cached downstream, because redirects send
`Cache-Control: no-store`. The row and its clicks are kept for investigation. This
also works for links with an alias or an expiry. A deactivated alias stays taken, so
it can't be claimed again.

---

## Data model, retention and privacy

### Tables

| Table | Contents | Key constraints / indexes |
|-------|----------|---------------------------|
| `links` | `id`, `code` (≤ 32: a generated base62 code or a client alias of `[A-Za-z0-9_-]`, case preserved), `url`, `is_active`, `created_at`, `deactivated_at`, `expires_at` (`DATETIME NULL`, naive UTC, `NULL` = never expires; new in CR-142) | `UNIQUE uq_links_code (code)`. No index on `expires_at`, which is only read from rows already loaded by code. |
| `click_events` | `id`, `link_id`, `created_at`, `referrer` (≤ 2048), `user_agent` (≤ 512), `ip_hash` (nullable) | `ix_click_events_link_id_created_at (link_id, created_at)` |
| `idempotency_keys` | `id`, `key` (≤ 255), `request_hash` (hex SHA-256 of the request payload, see below), `link_id`, `created_at` | `UNIQUE uq_idempotency_keys_key (key)` |

All timestamps, including `links.expires_at`, are stored as naive `DATETIME` values in
UTC. A link counts as expired when `expires_at <= now`. Expiry doesn't change
`is_active`.

`idempotency_keys.request_hash` is `sha256(url)` when the request had no `alias` and
no `expires_at`, which matches rows written by 1.0.0. Otherwise it is the SHA-256
of a canonical JSON object with `alias`, `expires_at` and `url` (see
[API.md](API.md#idempotency-semantics)). The table's DDL didn't change.

**MySQL collation caveat for aliases:** on SQLite and PostgreSQL, `links.code` compares
case-sensitively, so `MyLink` and `mylink` are two different links. MySQL's default
collations (`*_ci`) are case-insensitive. On MySQL, creating `mylink` when `MyLink`
exists fails with `409 Alias already in use`, and `GET /mylink` resolves to `MyLink`.
Generated codes are affected in the same way, but collisions there are retried. If you
need strict case-sensitivity on MySQL, give `links.code` a binary collation (e.g.
`utf8mb4_bin`). The application doesn't do this for you.

### Retention

- In this release **nothing is ever deleted**. `DELETE /api/links/{code}` is a soft
  delete, expired links stay in the table (they only stop redirecting), click events
  are kept forever, and idempotency keys never expire.
- There is no built-in purge job. To enforce a retention policy, run your own scheduled
  job against the database, for example deleting `click_events` older than N days.
  This also reduces stats history and `total_clicks`.
- Back up the database with your normal backup tooling. For SQLite, copy the
  `shortener.db` file while the service is stopped, or use `sqlite3 .backup`.

### Privacy

- **Raw client IPs are never stored or logged.** `click_events` has no IP column.
- If `IP_HASH_SALT` is set, `ip_hash` holds `HMAC-SHA256(IP_HASH_SALT, client_ip)` as
  hex. If it is unset or blank, `ip_hash` is `NULL`.
  - Treat the salt as a secret and inject it through your secret manager. Anyone who
    has the salt can confirm a guessed IP by brute force (the IPv4 space is small).
  - If you rotate the salt, new hashes can no longer be correlated with old ones.
    Unsetting it stops IP hashing completely.
  - The hashed address is the client address that the ASGI server reports. The app
    ignores `X-Forwarded-For`. Behind a reverse proxy, uvicorn's `--proxy-headers` /
    `--forwarded-allow-ips` options decide whether that is the proxy's address or
    the original client's.
- **Referrers and user agents are stored as sent.** Referrer URLs can contain personal
  data (for example in query strings), so include `click_events` in your
  data-protection assessment and retention policy.
- Target URLs in `links.url` are stored as submitted and are visible to anyone who
  knows the code.

---

## Scaling

- **Stateless:** the app keeps no in-memory state that matters for correctness. Code
  uniqueness and idempotency are enforced by database unique constraints. You can run
  several instances or workers behind a load balancer if they share **one
  PostgreSQL/MySQL database**.
- **SQLite:** use a single instance with a single worker only. SQLite serializes
  writes, so under concurrent load you get "database is locked" errors.
- **Moving to PostgreSQL:** install a driver and set, for example,
  `DATABASE_URL=postgresql+psycopg://user:pass@host:5432/shortener`. The ORM uses only
  portable column types, and the stats are computed in a portable way. The tables are
  created on first start. There is no built-in data migration from SQLite. On MySQL,
  note the [case-insensitive collation caveat](#data-model-retention-and-privacy) for
  aliases.
- **Schema changes and several instances:** apply the `links.expires_at` migration
  once before rolling out several instances (see
  [Rollout and rollback](#rollout-and-rollback)).
- **Concurrent first start:** each instance runs `create_all` at startup. If several
  instances start against an empty database at the same moment, one of them may fail
  with a "table already exists" error and need a restart. To avoid this, start one
  instance first or create the schema beforehand.
- **Workers:** endpoints are synchronous and run in the server's thread pool. Scale
  with `uvicorn --workers N` or more replicas. Size the database connection pool and
  `max_connections` to match. The engine uses SQLAlchemy's default pool size.
- **Hot path:** each redirect does one indexed lookup on `links.code` and one insert
  into `click_events`, in a separate small transaction. The expiry check uses the row
  that was already loaded and adds no query. Click recording is
  synchronous, and at very high traffic that insert is the bottleneck. Async or
  batched ingestion is a possible future improvement.
- **Stats cost:** `clicks_per_day` loads the timestamps of every click in the window
  (at most 365 days). The `(link_id, created_at)` index serves this query, but for
  very popular links the response gets slower as the window grows.
- **Caching:** don't put a cache in front of `GET /{code}`. Redirects send
  `Cache-Control: no-store` so that clicks are counted and deactivation takes effect
  immediately.

---

## Security notes

- **API key required for management:** every `/api/links*` endpoint (create,
  metadata, stats, deactivate) requires an `X-API-Key` header that matches the
  `API_KEY` setting. Otherwise the response is
  `401 {"detail": "Invalid or missing API key"}`. (Earlier versions of this runbook
  wrongly said there was no authentication.)
  - **Fail closed:** if `API_KEY` is unset, empty or whitespace-only, every
    `/api/links*` request is rejected. The management API is never open by accident.
    A key shorter than 16 characters stops the service at startup.
  - **Constant-time comparison** (`hmac.compare_digest`) prevents timing attacks on
    the key.
  - It is one shared key for all clients, with no per-client identity or scopes, and
    any key holder can deactivate any link. Treat it as a secret: inject it through
    your secret manager, never log it, and rotate it if it leaks.
  - `GET/HEAD /{code}`, `/healthz`, `/readyz` and the documentation routes
    (`/docs`, `/redoc`, `/openapi.json`) stay public. A short link exposes only its
    target URL.
- **Rate limiting / abuse protection** is not built in. Configure it at the reverse
  proxy or API gateway.
- **Open-redirect risk:** only `http`/`https` targets are accepted, so
  `javascript:`, `data:` and `file:` payloads are impossible in any letter case and
  with any whitespace or control-character tricks. Neither `alias` nor `expires_at`
  bypasses this validation. The service does not scan URL reputation, so use
  deactivation for takedowns. Custom aliases are readable and guessable by design.
  The metadata and stats behind them are still protected by the API key.
- **Reserved words:** `api`, `docs`, `redoc`, `healthz`, `readyz`, `metrics`,
  `openapi.json`, `static` and `favicon.ico` can't be taken as codes or aliases (in
  any letter case), so a short link can never imitate a service path.
- **Error responses** never expose stack traces, SQL or exception messages.
- **TLS** must be terminated at the proxy or load balancer. Set `BASE_URL` to the
  public `https://` origin so that `short_url` values are correct.
- CORS is not configured. Browsers on other origins can't call the API until you add
  CORS support.
