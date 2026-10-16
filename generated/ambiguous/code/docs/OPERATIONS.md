# Operations Runbook

URL Shortener operations runbook. It covers how to run, monitor and troubleshoot the
service in production, the campaign deployment profile and the path to PostgreSQL.

## Contents

- [Running the service](#running-the-service)
- [Startup and the automatic schema upgrade](#startup-and-the-automatic-schema-upgrade)
- [Health checks](#health-checks)
- [Logs](#logs)
- [Metrics](#metrics)
- [SQLite: WAL, backups and locking](#sqlite-wal-backups-and-locking)
- [Connection pool sizing](#connection-pool-sizing)
- [Failure modes and responses](#failure-modes-and-responses)
- [Data model, retention and privacy](#data-model-retention-and-privacy)
- [MySQL: case-sensitive short codes](#mysql-case-sensitive-short-codes)
- [Scaling](#scaling)
- [Security notes](#security-notes)
- [Campaign deployment](#campaign-deployment)
- [Scaling beyond the campaign profile](#scaling-beyond-the-campaign-profile)

---

## Running the service

```bash
pip install -r requirements.txt
export API_KEY='<secret of at least 16 characters, from your secret manager>'
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --no-access-log
```

**Always pass `--no-access-log`.** uvicorn's built-in access log prints the client IP
on every request, which breaks the service's privacy guarantee (raw IPs are never
logged or stored). The app writes its own request log without IPs (see [Logs](#logs)).

**Use exactly one worker (`--workers 1`) and one instance on SQLite.** See
[Scaling beyond the campaign profile](#scaling-beyond-the-campaign-profile) for
anything larger.

All settings are environment variables; see the configuration table in
[README.md](../README.md#configuration). The settings that matter most in operations:

| Variable | Default | Notes |
|----------|---------|-------|
| `API_KEY` | *(unset)* | Required for the management API (`/api/links*`). At least 16 characters. Unset means every management request gets 401. |
| `DATABASE_URL` | `sqlite:///./shortener.db` | Use an absolute path on local disk in production, e.g. `sqlite:////var/lib/shortener/shortener.db` (four slashes). |
| `SQLITE_BUSY_TIMEOUT_MS` | `5000` | 0–60000. Campaign value: `1000`. |
| `DB_POOL_SIZE` | `5` | ≥ 1. |
| `DB_MAX_OVERFLOW` | `10` | ≥ 0. |
| `DB_POOL_TIMEOUT_SECONDS` | `30.0` | > 0 and ≤ 300. |

---

## Startup and the automatic schema upgrade

Startup runs these steps in order:

1. `Settings` are read from environment variables. Invalid values such as
   `SHORT_CODE_LENGTH=2`, `LOG_LEVEL=verbose`, an `API_KEY` shorter than 16 characters,
   `DB_POOL_SIZE=0` or `SQLITE_BUSY_TIMEOUT_MS=60001` stop the process with a
   validation error. If `API_KEY` is unset, the WARNING
   `API_KEY is not configured: all /api/links requests will be rejected with 401` is
   logged (logger `app.security`) and the service starts with the management API
   locked.
2. The database engine is created with the pool settings (and, for file-based
   SQLite, the busy timeout and the WAL pragmas on every new connection). Importing
   `app.main` doesn't connect to the database.
3. During the ASGI lifespan startup, `Base.metadata.create_all` creates any missing
   tables and indexes. It never alters existing tables.
4. **Automatic schema upgrade:** the service inspects the `links` table and, if the
   `expires_at` column is missing (a database created by an earlier release), adds it
   as a nullable column. Existing rows get `NULL` (never expire). This step is
   idempotent and logs exactly one of these lines (logger `app.db`, field
   `schema_upgrade`):

   | Level | `message` | `schema_upgrade` | Meaning |
   |-------|-----------|------------------|---------|
   | INFO | `schema upgrade: links.expires_at added` | `added` | The column was missing and has been added. Logged once, on the first start after the upgrade. |
   | INFO | `schema upgrade: links.expires_at already present` | `already_present` | Nothing to do (fresh database or already upgraded). |
   | ERROR | `schema upgrade failed: could not add links.expires_at` | *(none)* | The `ALTER TABLE` failed. The traceback is in `exc`. **Startup aborts** and the process exits instead of serving requests. |

5. One INFO line `database tuning ...` reports the effective engine settings (see
   [The database tuning line](#the-database-tuning-line)).
6. The service starts serving requests. On shutdown, the engine's connection pool is
   disposed.

**Back up the database before deploying this release** (see
[Backups](#backups)). The upgrade is a single additive `ALTER TABLE`, but a backup
makes rollback trivial.

If the automatic upgrade fails (most often because the database user lacks `ALTER`
rights, or the SQLite file or its directory is read-only), fix the cause and restart,
or apply the equivalent SQL manually and restart:

```sql
-- SQLite and MySQL
ALTER TABLE links ADD COLUMN expires_at DATETIME;

-- PostgreSQL
ALTER TABLE links ADD COLUMN expires_at TIMESTAMP WITHOUT TIME ZONE;
```

For SQLite: `sqlite3 /var/lib/shortener/shortener.db 'ALTER TABLE links ADD COLUMN expires_at DATETIME;'`
(with the service stopped). After the manual change the next start logs
`schema upgrade: links.expires_at already present`.

Rolling back to the previous release after the upgrade is safe: the earlier code
ignores the extra nullable column. Expiry is then not enforced, and idempotency keys
that were created with an `expires_at` will not replay on the old code (it returns
409 for them).

### The database tuning line

One INFO line from logger `app.db`, emitted at startup after the schema upgrade. It
never contains the database URL or any credentials. The message lists the fields as
`key=value`, and the same fields are emitted as structured JSON keys:

```json
{"ts": "2025-11-20T08:00:00.512301+00:00", "level": "INFO", "logger": "app.db", "message": "database tuning dialect=sqlite pool_class=QueuePool pool_size=5 max_overflow=10 pool_timeout=30.0 journal_mode=wal synchronous=1 busy_timeout_ms=1000", "dialect": "sqlite", "pool_class": "QueuePool", "pool_size": 5, "max_overflow": 10, "pool_timeout": 30.0, "journal_mode": "wal", "synchronous": 1, "busy_timeout_ms": 1000}
```

| Field | Meaning | Expected for file-based SQLite |
|-------|---------|--------------------------------|
| `dialect` | SQLAlchemy dialect | `sqlite` |
| `pool_class` | Connection pool class | `QueuePool` |
| `pool_size` | `DB_POOL_SIZE` in effect (`None` for non-queue pools) | `5` |
| `max_overflow` | `DB_MAX_OVERFLOW` in effect | `10` |
| `pool_timeout` | `DB_POOL_TIMEOUT_SECONDS` in effect | `30.0` |
| `journal_mode` | SQLite only: `PRAGMA journal_mode` | **`wal`** |
| `synchronous` | SQLite only: `PRAGMA synchronous` (`0` OFF, `1` NORMAL, `2` FULL, `3` EXTRA) | **`1`** |
| `busy_timeout_ms` | SQLite only: `PRAGMA busy_timeout` | equals `SQLITE_BUSY_TIMEOUT_MS` |

For PostgreSQL/MySQL only the first five fields appear. For in-memory SQLite (tests),
`pool_class` is `StaticPool`, the pool fields are `None` and WAL is not used.

If a file-based SQLite database shows a `journal_mode` other than `wal`, the database
directory is probably not writable or not on a local filesystem; fix that before
going live.

---

## Health checks

| Endpoint | Purpose | Touches DB | Healthy | Unhealthy |
|----------|---------|-----------|---------|-----------|
| `GET /healthz` | Liveness: the process is serving HTTP | No | `200 {"status":"ok"}` | No response or timeout |
| `HEAD /healthz` | Liveness for HEAD-based monitors | No | `200`, empty body | No response or timeout |
| `GET /readyz` | Readiness: the database accepts queries (`SELECT 1`) | Yes | `200 {"status":"ready"}` | `503 {"detail":"Database is not ready"}` (or `"Service temporarily unavailable"`) |
| `HEAD /readyz` | Readiness for HEAD-based monitors | Yes | `200`, empty body | `503`, empty body |

The probes are public (no API key). `HEAD` routes are not in the OpenAPI schema;
earlier releases sent `HEAD` probes to the catch-all redirect route (which answered
`404`), so monitors configured with `HEAD` now work.

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

External uptime monitor (any HTTP monitor that probes with `HEAD`):

| Setting | Value |
|---------|-------|
| URL | `https://<public host>/readyz` |
| Method | `HEAD` (or `GET`) |
| Expected status | `200` |
| Interval | 60 s |
| Timeout | 5 s |
| Alert after | 2 consecutive failures |

Optionally add a second monitor on `HEAD /healthz` to distinguish "process down" from
"database down".

Don't use `/readyz` as the liveness probe. A database outage would then restart every
pod without fixing anything.

```bash
curl -I https://<public host>/healthz   # HTTP/1.1 200 OK
curl -I https://<public host>/readyz    # HTTP/1.1 200 OK
```

---

## Logs

The `app` logger hierarchy writes **one JSON object per line to stderr**. `LOG_LEVEL`
sets the level, and the default is `INFO`.

Common keys: `ts` (ISO 8601 UTC), `level`, `logger`, `message`. Request lines also
have `method`, `path`, `status` and `duration_ms`. The startup tuning line has
`dialect`, `pool_class`, `pool_size`, `max_overflow`, `pool_timeout` and, for SQLite,
`journal_mode`, `synchronous` and `busy_timeout_ms`. Schema-upgrade lines have
`schema_upgrade`. Error lines include the formatted traceback under `exc`.

### Log events

| Logger | Level | `message` | Meaning |
|--------|-------|-----------|---------|
| `app.request` | INFO | `request` | One line per HTTP request, with `method`, `path` (no query string), `status` and `duration_ms`. |
| `app.db` | INFO | `database tuning dialect=... pool_class=... ...` | Startup: effective engine tuning. See [The database tuning line](#the-database-tuning-line). |
| `app.db` | INFO | `schema upgrade: links.expires_at added` | Startup: the `expires_at` column was added (`schema_upgrade=added`). |
| `app.db` | INFO | `schema upgrade: links.expires_at already present` | Startup: no upgrade needed (`schema_upgrade=already_present`). |
| `app.db` | ERROR | `schema upgrade failed: could not add links.expires_at` | Startup aborted. The traceback is in `exc`. |
| `app.security` | WARNING | `API_KEY is not configured: all /api/links requests will be rejected with 401` | Startup without `API_KEY`. The management API is locked (fail closed). |
| `app.errors` | ERROR | `unhandled exception` | An unexpected exception. The client got `500 {"detail":"Internal Server Error"}`. The traceback is in `exc`. |
| `app.errors` | ERROR | `database error` | A `SQLAlchemyError` reached the error handler (including a SQLite lock that outlasted the busy timeout, or a pool timeout). The client got `503 {"detail":"Service temporarily unavailable"}`. The traceback is in `exc`. |
| `app.health` | WARNING | `readiness check failed` | `/readyz` returned 503. |
| `app.services.links` | WARNING | `failed to record click for link_id=<id> (<ExceptionClass>)` | A click event could not be written, e.g. `failed to record click for link_id=17 (OperationalError)`. The click is dropped; the redirect still returned 307. No traceback, exception message or IP is logged. |
| `app.services.links` | ERROR | `short code allocation exhausted` | Every code-generation attempt collided or was reserved. The client got 503. |

Sample lines:

```json
{"ts": "2025-01-15T10:30:00.123456+00:00", "level": "INFO", "logger": "app.request", "message": "request", "method": "GET", "path": "/aB3xY9Q", "status": 307, "duration_ms": 3.41}
{"ts": "2025-01-15T10:31:12.004211+00:00", "level": "WARNING", "logger": "app.services.links", "message": "failed to record click for link_id=17 (OperationalError)"}
{"ts": "2025-01-15T08:00:00.401122+00:00", "level": "INFO", "logger": "app.db", "message": "schema upgrade: links.expires_at added", "schema_upgrade": "added"}
```

### What is never logged

The app's logs never contain client IPs, `X-Forwarded-For`, request headers
(including `X-API-Key`), the API key itself, the database URL, query strings or
request bodies. Exception details go only to the server-side log, never to HTTP
responses. Logs from uvicorn itself (`uvicorn.error`, and the access log if enabled)
are separate and do not use the JSON format.

---

## Metrics

There is **no metrics endpoint** (no Prometheus `/metrics`). You can derive these
signals from the log lines with your log pipeline:

| Signal | Source | Suggested alert |
|--------|--------|-----------------|
| Request rate per route | count of `app.request` grouped by `method` and `path` prefix (`/api/links`, `/{code}`) | n/a |
| Redirect latency p95 | `duration_ms` where `status = 307` | p95 > 100 ms sustained (campaign target) |
| Create latency p95 | `duration_ms` where `method = POST` and `path = /api/links` | p95 > 100 ms sustained |
| 5xx rate | `status >= 500` | any sustained 500s; 503 rate > 1 % |
| 401 rate | `status = 401` on `/api/links*` | spikes (misconfigured client or key probing) |
| Lost clicks | count of WARNING `failed to record click` per hour | > 0 sustained |
| Expired-link hits | count of `status = 410` on `/{code}` | informational after the campaign end |
| Code exhaustion | count of `short code allocation exhausted` | any |
| Readiness | `/readyz` probe result / `readiness check failed` | failing for more than 1 minute |

`duration_ms` is measured inside the app and excludes network time; note that the
request log does not include the query string. You can query the database directly
for business metrics, or use `GET /api/links/{code}/stats/hourly` for clicks per hour.

---

## SQLite: WAL, backups and locking

### WAL mode

Every new connection to a file-based SQLite database runs
`PRAGMA journal_mode=WAL` and `PRAGMA synchronous=NORMAL`. The busy timeout is set
from `SQLITE_BUSY_TIMEOUT_MS`. The startup tuning line shows the effective values
(`journal_mode=wal synchronous=1 busy_timeout_ms=...`).

- **Sidecar files:** in WAL mode SQLite keeps two extra files next to the database:
  `shortener.db-wal` (the write-ahead log) and `shortener.db-shm` (shared-memory
  index). They are part of the database. Never delete them while the service is
  running, and treat all three files as one unit.
- **Local, writable directory:** the directory that holds the database must be
  writable by the service user (SQLite creates and removes the sidecar files there),
  and it must be on a **local disk**.
- **No network filesystems:** do not put the database on NFS, SMB/CIFS or other
  network or shared filesystems. WAL relies on shared memory and file locking that
  these do not provide reliably, which can corrupt the database.
- **Durability:** with `synchronous=NORMAL` in WAL mode, an application crash never
  loses committed data. A power loss or OS crash can lose the last few committed
  transactions, but does not corrupt the database.
- The `-wal` file is checkpointed back into the main file automatically and can
  grow temporarily under sustained load; this is normal.

### Backups

A copy of `shortener.db` alone, taken while the service runs, can miss committed data
that is still in `shortener.db-wal`, or be inconsistent. Use one of these methods:

1. **Online (service running):** use the SQLite backup API, which produces a
   consistent single-file copy:

   ```bash
   sqlite3 /var/lib/shortener/shortener.db ".backup '/var/backups/shortener/shortener-$(date -u +%Y%m%dT%H%M%SZ).db'"
   ```

2. **Offline (service stopped):** stop the service, then copy **all three files**
   together: `shortener.db`, `shortener.db-wal` and `shortener.db-shm` (the sidecars
   may be absent after a clean shutdown; copy them if they exist).

To restore: stop the service, remove the current `shortener.db`, `shortener.db-wal`
and `shortener.db-shm`, put the backup in place as `shortener.db` (plus its own
`-wal`/`-shm` files if you took an offline copy), make sure the service user owns
them, and start the service.

### Locking behaviour

SQLite allows many readers and **one writer at a time**. Under WAL:

- **Readers are not blocked.** Redirect lookups, metadata reads and stats run while a
  write is in progress, and writers are not blocked by readers. The redirect ends its
  read transaction before it writes the click.
- **Click writes** (one `INSERT` per redirect) wait for the write lock for up to
  `SQLITE_BUSY_TIMEOUT_MS`. If the lock is still held after that, the click is
  **dropped**, one WARNING `failed to record click for link_id=<id> (OperationalError)`
  is logged, and **the redirect still returns 307**. The extra latency is bounded by
  the busy timeout.
- **Creates, PATCH and DELETE** also wait up to `SQLITE_BUSY_TIMEOUT_MS`. After that
  they fail with `503 {"detail":"Service temporarily unavailable"}` and an ERROR
  `database error` line. Clients can retry; use an `Idempotency-Key` on creates.
- **If the link lookup itself fails** (for example the database file is missing or
  unreadable, the disk fails, or no pool connection becomes free within
  `DB_POOL_TIMEOUT_SECONDS`), the redirect returns
  `503 {"detail":"Service temporarily unavailable"}`.
- Within one process, concurrent writes (click inserts, creates, PATCH, DELETE) are
  serialised by the write lock. Each write transaction is short, so they normally
  wait only milliseconds. Long waits and timeouts usually mean that another
  **process** holds the lock (an extra worker, a second instance, a manual `sqlite3`
  session or a batch job), or that a sustained write burst is in progress. Run
  exactly one uvicorn worker and one instance on SQLite. Do not run batch jobs that
  hold long write transactions against the live file.

Tuning the busy timeout is a trade-off: a lower value (campaign: `1000` ms) keeps
redirect latency bounded during write bursts but drops more clicks; a higher value
(default `5000` ms) drops fewer clicks but can stall individual redirects longer.

---

## Connection pool sizing

The engine uses a `QueuePool` for file-based SQLite and for server databases:

- `DB_POOL_SIZE` (default 5) connections are kept open per process.
- Up to `DB_MAX_OVERFLOW` (default 10) extra connections are opened under load and
  closed when idle, so one process uses at most `DB_POOL_SIZE + DB_MAX_OVERFLOW`
  (default 15) connections.
- Each request holds one connection while it runs. FastAPI runs the synchronous
  endpoints in a thread pool, so concurrency is bounded by the pool. When every
  connection is busy, a request waits up to `DB_POOL_TIMEOUT_SECONDS` (default 30)
  and then fails with 503 (`database error` in the log).
- In-memory SQLite (`sqlite://`, used by tests) ignores these settings. It uses a
  `StaticPool`: one connection shared by all threads, so every request sees the same
  in-memory database.

Guidance:

- **SQLite, one worker:** the defaults (5 + 10) are enough for ~50 RPS, since each
  redirect holds a connection for a few milliseconds. More connections do not add
  write throughput on SQLite (there is a single writer).
- **Server databases:** the total across all processes must fit the server:
  `instances × workers × (DB_POOL_SIZE + DB_MAX_OVERFLOW) ≤ max_connections`, minus
  connections reserved for administration, migrations and monitoring.
- A lower `DB_POOL_TIMEOUT_SECONDS` makes overload visible faster (503 instead of
  slow responses); keep it above your normal p99 latency.

---

## Failure modes and responses

### Database unreachable or erroring

- **Symptoms:** `/readyz` returns 503. API calls and redirects return
  `503 {"detail":"Service temporarily unavailable"}`. ERROR `database error` lines
  appear in the log.
- **Response:** check database availability, credentials in `DATABASE_URL`, network
  access, disk space and the connection limit. For SQLite, check that the file, its
  `-wal`/`-shm` files and the directory are present and writable by the service user.
  On non-SQLite backends, `pool_pre_ping=True` means the pool recovers from dropped
  connections by itself once the database is back. No restart is needed.

### SQLite "database is locked"

- **Cause:** a write waited longer than `SQLITE_BUSY_TIMEOUT_MS` for the single write
  lock, usually because several processes (extra workers, a second instance, a manual
  `sqlite3` session or batch job) write to the same file, or because of a sustained
  write burst.
- **Symptoms:** under WAL, redirects keep returning 307 and reads keep working.
  WARNING `failed to record click for link_id=<id> (OperationalError)` lines appear
  (dropped clicks), and `POST`/`PATCH`/`DELETE` on `/api/links*` may return 503 with
  ERROR `database error`.
- **Response:**
  1. Check that exactly one uvicorn process serves this database
     (`--workers 1`, one instance) and that no other process holds a write
     transaction.
  2. Check the startup tuning line: `journal_mode=wal`, and `busy_timeout_ms` as
     intended.
  3. If clicks drop during normal bursts, raise `SQLITE_BUSY_TIMEOUT_MS` (accepting
     longer worst-case redirect latency).
  4. If load exceeds the single-instance profile, move to PostgreSQL (see
     [Scaling beyond the campaign profile](#scaling-beyond-the-campaign-profile)).

### Clicks not being recorded

- **Symptoms:** redirects work but `total_clicks` stops increasing, and the log shows
  WARNING `failed to record click for link_id=... (<ExceptionClass>)`.
- **Explanation:** click recording is best-effort by design. The redirect is never
  failed because of analytics. The exception class name points at the cause
  (`OperationalError`: a locked database, a full disk or a read-only file;
  other classes: check for manual schema changes). No traceback is logged for this
  event, so the exception message (which can contain request data) never reaches the
  log.
- **Response:** fix the underlying database problem. Clicks lost during the incident
  can't be recovered. Count the WARNING lines per hour to estimate the loss.

### Redirects return 410 "Link has expired" unexpectedly

- **Cause:** the link's `expires_at` has been reached. Check it with
  `GET /api/links/{code}` (`expires_at`, `is_expired`). A common mistake is a wrong
  year, or a local time sent without an offset (it is treated as UTC).
- **Response:** set the correct instant with `PATCH /api/links/{code}`
  (`{"expires_at": "..."}`) or clear it (`{"expires_at": null}`). The change takes
  effect on the next request; 410s are sent with `Cache-Control: no-store`, so nothing
  is cached downstream.

### 401 "Invalid or missing API key"

- **Cause:** the client sent no `X-API-Key`, a wrong one, or the server has no
  `API_KEY` configured (look for the startup WARNING
  `API_KEY is not configured ...`).
- **Response:** configure `API_KEY` (≥ 16 characters) on the server and the same
  value in the client, then restart the service. Redirects and probes are unaffected.

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

### 409 responses on POST with Idempotency-Key

- This is expected when a client reuses a key with a different `url` or a different
  `expires_at` (added, changed or removed). If clients report it unexpectedly, they
  are probably generating keys that aren't unique. Keys are global and never expire.
- `"Idempotency-Key conflict, please retry"` is a rare race outcome. Retrying the
  request is safe.

### Startup fails

- The process exits with a settings validation error: fix the offending environment
  variable (for example an `API_KEY` shorter than 16 characters, or a pool setting
  out of range).
- `create_all` fails because of database errors or permissions: the database user
  needs `CREATE TABLE` / `CREATE INDEX` rights on first start. With a SQLite file
  path, the directory must be writable.
- `schema upgrade failed: could not add links.expires_at`: the database user needs
  `ALTER TABLE` rights, or the SQLite file/directory is read-only. Fix it, or apply
  the manual SQL from
  [Startup and the automatic schema upgrade](#startup-and-the-automatic-schema-upgrade),
  and restart.
- `ModuleNotFoundError` for a database driver: install the driver that matches
  `DATABASE_URL` (e.g. `psycopg[binary]`, `pymysql`). Only SQLite works out of the
  box.

### Abuse / phishing takedown

Deactivate the link with `DELETE /api/links/{code}` (with `X-API-Key`). It takes
effect immediately: redirects return `410 Link has been deactivated` and nothing is
cached downstream, because redirect responses send `Cache-Control: no-store`. The row
and its clicks are kept for investigation. Deactivation cannot be undone by PATCH.

---

## Data model, retention and privacy

### Tables

| Table | Contents | Key constraints / indexes |
|-------|----------|---------------------------|
| `links` | `id`, `code`, `url`, `is_active`, `created_at`, `deactivated_at`, `expires_at` (nullable; `NULL` = never expires) | `UNIQUE uq_links_code (code)`; on MySQL/MariaDB `code` uses `utf8mb4_bin` |
| `click_events` | `id`, `link_id`, `created_at`, `referrer` (≤ 2048), `user_agent` (≤ 512), `ip_hash` (nullable) | `ix_click_events_link_id_created_at (link_id, created_at)`, which also serves the daily and hourly stats queries |
| `idempotency_keys` | `id`, `key` (≤ 255), `request_hash` (SHA-256 of the URL, or of the URL plus the UTC `expires_at` when one was sent), `link_id`, `created_at` | `UNIQUE uq_idempotency_keys_key (key)` |

All timestamps, including `expires_at`, are stored as naive `DATETIME` values in UTC.
`links.expires_at` is added to existing databases by the
[automatic schema upgrade](#startup-and-the-automatic-schema-upgrade).

### Retention

- In this release **nothing is ever deleted**. `DELETE /api/links/{code}` is a soft
  delete, expired links stay in the table, click events are kept forever, and
  idempotency keys never expire.
- There is no built-in purge job. To enforce a retention policy, run your own scheduled
  job against the database, for example deleting `click_events` older than N days.
  This also reduces stats history and `total_clicks`. On SQLite, run it during a quiet
  period with short transactions, because it holds the write lock.
- Back up the database regularly. For SQLite, follow [Backups](#backups).

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
- Target URLs in `links.url` are stored as submitted. Anyone who knows a code can
  reach the target through the public redirect; metadata and stats need the API key.

---

## MySQL: case-sensitive short codes

Short codes are case-sensitive (`AbC1234` and `abc1234` are different links). SQLite
and PostgreSQL compare `VARCHAR` values case-sensitively. MySQL/MariaDB default
collations are case-insensitive, so on a fresh MySQL/MariaDB database the service
creates `links.code` with the binary collation `utf8mb4_bin`.

Tables created on MySQL/MariaDB by an earlier release keep their old collation.
Without the fix, lookups match codes case-insensitively and the unique constraint
treats case variants as duplicates. Apply this once (back up first, preferably during
a quiet period):

```sql
ALTER TABLE links MODIFY code VARCHAR(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL;
```

Check it with `SHOW FULL COLUMNS FROM links LIKE 'code';` (the `Collation` column must
show `utf8mb4_bin`). The production campaign deployment runs on SQLite, where no
action is needed.

---

## Scaling

- **Stateless app:** the app keeps no in-memory state that matters for correctness
  (no link cache). Code uniqueness and idempotency are enforced by database unique
  constraints. On a server database (PostgreSQL), several instances or workers can
  share one database behind a load balancer.
- **SQLite:** one instance with one worker only. See
  [Campaign deployment](#campaign-deployment) for the supported profile and
  [Scaling beyond the campaign profile](#scaling-beyond-the-campaign-profile) for
  anything larger.
- **Concurrent first start:** each instance runs `create_all` and the schema upgrade at
  startup. If several instances start against an empty or not-yet-upgraded database
  at the same moment, one of them may fail and need a restart. To avoid this, start
  one instance first, wait for the schema log line, then start the rest.
- **Hot path:** each redirect does one narrow indexed `SELECT` on `links.code` (id,
  url, is_active, expires_at), ends that read transaction, then inserts one row into
  `click_events` in its own small transaction. Click recording is synchronous; at
  very high traffic that insert is the bottleneck. Asynchronous or batched ingestion
  is a possible future improvement.
- **Stats cost:** daily and hourly stats are aggregated in the database with
  `GROUP BY` over the `(link_id, created_at)` index, so memory use is proportional to
  the number of days or hours, not the number of clicks. Hourly ranges are capped at
  744 hours.
- **Caching:** don't put a cache in front of `GET /{code}`. Redirects (and their
  404/410 errors) send `Cache-Control: no-store` so that clicks are counted and
  deactivation, expiry and PATCH changes take effect immediately.

---

## Security notes

- **API-key authentication:** every `/api/links*` endpoint (create, lookup, PATCH,
  deactivate, daily and hourly stats) requires the `X-API-Key` header to equal
  `API_KEY`. The comparison runs in constant time. Missing or wrong keys get
  `401 {"detail":"Invalid or missing API key"}` with `WWW-Authenticate: ApiKey`.
- **Fail closed:** if `API_KEY` is unset or blank, every `/api/links*` request is
  rejected with 401 and a startup WARNING is logged. The management API is never
  exposed by accident.
- **Key strength and handling:** `API_KEY` must be at least 16 characters (startup
  fails otherwise); use a random value of 32+ characters, e.g.
  `python -c 'import secrets; print(secrets.token_urlsafe(32))'`. Inject it from a
  secret manager, never commit it, and share it only with the systems that manage
  links. To rotate it, set the new value, restart the service and update the clients;
  there is a short window in which clients with the old key get 401. The key is never
  logged.
- **Public endpoints:** `GET`/`HEAD /{code}`, `GET`/`HEAD /healthz` and
  `GET`/`HEAD /readyz` need no key, by design.
- **Rate limiting / abuse protection** is not built in. Configure it at the reverse
  proxy or API gateway, especially for the public redirect and for repeated 401s.
- **Open-redirect risk:** only `http`/`https` targets are accepted, so
  `javascript:`, `data:` and `file:` payloads are impossible. The service does not
  scan URL reputation, so use deactivation for takedowns.
- **Error responses** never expose stack traces, SQL or exception messages.
- **TLS** must be terminated at the proxy or load balancer, so that `X-API-Key` never
  travels in clear text. Set `BASE_URL` to the public `https://` origin so that
  `short_url` values are correct.
- CORS is not configured. Browsers on other origins can't call the API until you add
  CORS support.

---

## Campaign deployment

This is the supported production profile for the TV campaign.

### Targets

| Target | Value |
|--------|-------|
| Throughput | ~50 requests per second (mostly redirects) |
| Redirect latency | p95 < 100 ms, measured as `duration_ms` of `status = 307` request lines |
| Availability | Best effort; one instance, no failover |
| Instances | One |

### Topology

- **One instance** (one VM or container) behind the TLS-terminating proxy/load
  balancer.
- **One uvicorn worker:** `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --no-access-log`.
- **SQLite in WAL mode on local disk**, e.g. `/var/lib/shortener/shortener.db` on the
  instance's own disk (not NFS/SMB, not a shared volume), in a directory writable by
  the service user.

### Configuration

```bash
export DATABASE_URL='sqlite:////var/lib/shortener/shortener.db'
export BASE_URL='https://<public short-link host>'
export API_KEY='<from the secret manager, 32+ random characters>'
export SQLITE_BUSY_TIMEOUT_MS=1000
export DB_POOL_SIZE=5
export DB_MAX_OVERFLOW=10
export DB_POOL_TIMEOUT_SECONDS=30
export LOG_LEVEL=INFO
# optional: export IP_HASH_SALT='<from the secret manager>'

uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --no-access-log
```

| Setting | Recommended | Why |
|---------|-------------|-----|
| `SQLITE_BUSY_TIMEOUT_MS` | `1000` | Bounds the extra redirect latency from a write burst to 1 s; a click that cannot be written within 1 s is dropped with a WARNING instead of stalling the redirect. |
| `DB_POOL_SIZE` | `5` | Enough connections for ~50 RPS of short requests. |
| `DB_MAX_OVERFLOW` | `10` | Head-room for bursts (at most 15 connections). |
| `DB_POOL_TIMEOUT_SECONDS` | `30` | Default; fail with 503 rather than queue forever. |

### Setting the campaign expiry

Every campaign link must stop redirecting at the **confirmed campaign end**. The
example below uses `2025-12-31T23:59:00Z`; **confirm the exact instant, the time zone
and the year with marketing** before applying it. Always send the value with `Z` or an
explicit offset: a value without an offset is treated as UTC. For example, "midnight
at the end of 31 December, Berlin time" is `2026-01-01T00:00:00+01:00`, which is
`2025-12-31T23:00:00Z`. The link returns `410 Link has expired` from that instant
onwards.

```bash
export BASE='https://<public short-link host>'
export CAMPAIGN_END='2025-12-31T23:59:00Z'   # confirmed with marketing
```

**New links (POST):** include `expires_at` when creating them (use an
`Idempotency-Key` so retries are safe):

```bash
curl -s -X POST "$BASE/api/links" \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: campaign-tv-spot-1' \
  -d "{\"url\": \"https://example.com/campaign?src=tv1\", \"expires_at\": \"$CAMPAIGN_END\"}"
```

**Existing links (PATCH loop):** put the campaign codes in `campaign-codes.txt`, one
per line, and run:

```bash
while read -r code; do
  [ -z "$code" ] && continue
  status=$(curl -s -o /dev/null -w '%{http_code}' -X PATCH "$BASE/api/links/$code" \
    -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
    -d "{\"expires_at\": \"$CAMPAIGN_END\"}")
  echo "$code $status"
done < campaign-codes.txt
```

Every line must end in `200`. `404` means a wrong code in the list; `422` means the
instant is not in the future or is malformed. PATCH can be re-run safely and only
changes `expires_at`; it never reactivates a deactivated link.

**Verify (GET):** check `expires_at` and `is_expired` for every code (uses `jq`):

```bash
while read -r code; do
  [ -z "$code" ] && continue
  curl -s -H "X-API-Key: $API_KEY" "$BASE/api/links/$code" \
    | jq -r '[.code, .expires_at, .is_expired, .is_active] | @tsv'
done < campaign-codes.txt
```

Expected for every link before the end: `<code>  2025-12-31T23:59:00Z  false  true`.
To correct a wrong instant later, re-run the PATCH loop with the right value; to
remove the expiry, send `{"expires_at": null}`.

### Pulling hourly clicks per TV slot

Use `GET /api/links/{code}/stats/hourly` with the slot window converted to **UTC**.
Buckets are whole UTC hours: `start` is floored and `end` is rounded up to the hour,
`end` is exclusive, ranges are capped at 744 hours, and hours without clicks are
returned with `count: 0`. Pull a window that includes the response tail after the
slot (e.g. one or two hours after it).

Conversion examples (check daylight-saving time for the broadcast date):

| Broadcast (local) | Zone offset | Slot in UTC | Query (`start` / `end`) | Buckets returned |
|-------------------|-------------|-------------|-------------------------|------------------|
| 20 Nov 2025, 20:15–20:45 Berlin (CET) | `+01:00` | 19:15–19:45Z | `start=2025-11-20T19:00:00Z&end=2025-11-20T21:00:00Z` | 19:00Z, 20:00Z |
| same slot, sent with the local offset | `+01:00` | 19:15–19:45Z | `start=2025-11-20T20:00:00%2B01:00&end=2025-11-20T22:00:00%2B01:00` | 19:00Z, 20:00Z |
| 20 Nov 2025, 21:00–21:30 India (IST) | `+05:30` | 15:30–16:00Z | `start=2025-11-20T21:00:00%2B05:30&end=2025-11-20T23:00:00%2B05:30` | 15:00Z, 16:00Z, 17:00Z (start floored from 15:30Z, end rounded up from 17:30Z) |
| 20 Nov 2025, 20:00–20:30 New York (EST) | `-05:00` | 01:00–01:30Z on 21 Nov | `start=2025-11-20T20:00:00-05:00&end=2025-11-20T22:00:00-05:00` | 01:00Z, 02:00Z (21 Nov) |

Notes:

- **Encode `+` as `%2B`** in the query string (a literal `+` becomes a space and the
  request fails with 422). `-` offsets and `Z` need no encoding. Or let curl encode:

  ```bash
  curl -s -G -H "X-API-Key: $API_KEY" "$BASE/api/links/$code/stats/hourly" \
    --data-urlencode 'start=2025-11-20T21:00:00+05:30' \
    --data-urlencode 'end=2025-11-20T23:00:00+05:30'
  ```

- **Half-hour zones** (e.g. India `+05:30`, Newfoundland `-03:30`): UTC hour buckets
  start at hh:30 local time, so the bucket `15:00Z` covers 20:30–21:30 IST. A slot at
  21:00–21:30 IST falls in that bucket together with the half hour before it; compare
  against the same bucket on a day without a broadcast as a baseline.
- `total_clicks` in the hourly response is the sum over the requested range only.
- For all campaign links and one slot:

  ```bash
  while read -r code; do
    [ -z "$code" ] && continue
    curl -s -H "X-API-Key: $API_KEY" \
      "$BASE/api/links/$code/stats/hourly?start=2025-11-20T19:00:00Z&end=2025-11-20T21:00:00Z" \
      | jq -r '.code as $c | .clicks_per_hour[] | [$c, .hour, .count] | @tsv'
  done < campaign-codes.txt
  ```

Hourly counts cover recorded clicks only; check the WARNING
`failed to record click` count for the same hours to see whether any were dropped.

### Pre-launch checklist

- [ ] The service runs with `--workers 1 --no-access-log` on one instance, with the
      database on a local, writable disk.
- [ ] The startup log shows the tuning line with `dialect=sqlite pool_class=QueuePool
      pool_size=5 max_overflow=10 pool_timeout=30.0 journal_mode=wal synchronous=1
      busy_timeout_ms=1000`.
- [ ] The startup log shows `schema upgrade: links.expires_at added` (first start after
      the deploy) or `schema upgrade: links.expires_at already present`, and no
      `schema upgrade failed` line.
- [ ] No `API_KEY is not configured` WARNING; a request with the key succeeds and one
      without it gets 401.
- [ ] `curl -I $BASE/healthz` and `curl -I $BASE/readyz` both return `200`, and the
      external monitor on `HEAD /readyz` (and optionally `HEAD /healthz`) is configured
      and green.
- [ ] A backup was taken before the deploy (`sqlite3 ... ".backup ..."`) and its
      location is recorded.
- [ ] Expiry verified on **every** campaign link: the GET loop shows the confirmed
      `expires_at` and `is_expired=false` for each code.
- [ ] The hourly stats query returns data for a test link after a test click (the
      current hour shows `count >= 1`).
- [ ] A smoke load test at ~50 RPS against a test link shows redirect p95 < 100 ms
      (from `duration_ms` of `status = 307` lines or the load tool) and no 5xx.
- [ ] Alerts are set for 5xx rate, `failed to record click` WARNINGs and readiness.

### Restart and recovery

**Planned restart:**

1. Take a backup (`sqlite3 /var/lib/shortener/shortener.db ".backup '...'"`).
2. Restart the service (e.g. `systemctl restart shortener` or restart the container).
   Do not start a second process against the same file.
3. Check the startup lines (tuning line with `journal_mode=wal`, schema line
   `already present`), then `curl -I $BASE/readyz` returns `200`.
4. Open a test short link and confirm the 307.

**Unplanned crash:** start the service again. SQLite recovers the WAL automatically
on the next open; committed transactions are kept. Do not delete `-wal`/`-shm` files
before starting. Then follow steps 3–4 above.

**`/readyz` fails or redirects return 503:**

1. Check disk space (`df -h /var/lib/shortener`), file ownership and permissions of
   `shortener.db`, `shortener.db-wal`, `shortener.db-shm` and the directory.
2. Check that only one process uses the file (`lsof /var/lib/shortener/shortener.db`).
3. Look for ERROR `database error` lines and their `exc` traceback.
4. Fix the cause and restart.

**Startup aborts with `schema upgrade failed: could not add links.expires_at`:** check
write permissions, apply the manual `ALTER TABLE links ADD COLUMN expires_at DATETIME;`
with the service stopped if needed, and restart.

**Database corrupted or lost:** stop the service, restore the latest backup as
described in [Backups](#backups), start the service and verify as above. Links and
clicks created after the backup are lost; re-run the campaign expiry PATCH and verify
loops if expiries were changed after the backup.

**Rollback of the release:** stop the service, deploy the previous version and start
it. The `expires_at` column stays in place and is ignored by the old code (expiry is
not enforced). If needed, restore the pre-deploy backup instead.

---

## Scaling beyond the campaign profile

The SQLite profile above supports exactly one instance with one worker at around
50 RPS. **Running multiple uvicorn workers, multiple replicas, or more than ~50 RPS
requires PostgreSQL.** SQLite has a single writer per file and cannot be shared safely
between hosts.

**No PostgreSQL driver ships in this release**; `requirements.txt` contains only what
SQLite needs. Steps to migrate:

1. **Add and pin a driver.** Add an exact version of a PostgreSQL driver to
   `requirements.txt`, e.g. `psycopg[binary]==<exact version>`, install it and run the
   test suite.
2. **Set `DATABASE_URL`**, e.g.
   `DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>:5432/shortener`. Inject
   credentials from the secret manager. Non-SQLite engines use `pool_pre_ping=True`
   and the `DB_POOL_*` settings; `SQLITE_BUSY_TIMEOUT_MS` is ignored.
3. **Create the schema on a fresh database by starting the app** once (a single
   instance) against the empty PostgreSQL database. `create_all` creates the tables
   and indexes, and the log shows
   `schema upgrade: links.expires_at already present`. Stop it again.
4. **Copy the data.** Stop the SQLite service (or take a final `.backup`), export
   the tables in foreign-key order (`links`, then `click_events`, then
   `idempotency_keys`) keeping their `id` values, import them, then reset the
   sequences. For example, with CSV:

   ```bash
   for t in links click_events idempotency_keys; do
     sqlite3 -header -csv /var/lib/shortener/shortener.db "SELECT * FROM $t;" > "$t.csv"
   done
   ```

   ```sql
   -- in psql, connected to the new database
   \copy links FROM 'links.csv' WITH (FORMAT csv, HEADER true)
   \copy click_events FROM 'click_events.csv' WITH (FORMAT csv, HEADER true)
   \copy idempotency_keys FROM 'idempotency_keys.csv' WITH (FORMAT csv, HEADER true)
   SELECT setval(pg_get_serial_sequence('links', 'id'), COALESCE(MAX(id), 1)) FROM links;
   SELECT setval(pg_get_serial_sequence('click_events', 'id'), COALESCE(MAX(id), 1)) FROM click_events;
   SELECT setval(pg_get_serial_sequence('idempotency_keys', 'id'), COALESCE(MAX(id), 1)) FROM idempotency_keys;
   ```

   Compare row counts per table between the two databases, and spot-check a few links
   with `GET /api/links/{code}` (including `expires_at`) and a redirect.
5. **Size the pool per worker.** Every worker process has its own pool, so:
   `instances × workers × (DB_POOL_SIZE + DB_MAX_OVERFLOW) ≤ max_connections` of the
   PostgreSQL server (leave head-room for admin and monitoring connections). For
   example, 2 replicas × 4 workers × (5 + 10) = 120 connections.
6. Start one instance first, wait for the startup lines and `/readyz`, then scale out
   to more workers (`--workers N`) and replicas.
