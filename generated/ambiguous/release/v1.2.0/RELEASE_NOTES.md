# Release v1.2.0 — Campaign readiness: hardened single-instance SQLite redirects (50 RPS / p95 < 100 ms), automatic link expiry at campaign end, hourly click analytics, trustworthy probes, and accurate docs

Next week marketing starts a billboard and TV campaign that will show our short links to the public. The product owner has set the targets (Q-1): peak load is modest, up to about 50 redirect requests per second. Redirect p95 latency must be under 100 ms. Availability is best effort on one instance. Production stays on SQLite, hardened with WAL, a busy_timeout and a configurable pool (Q-2). No new database driver is added. The move to PostgreSQL for multi-worker or larger scale is documented but not implemented.

Amendment amd_5305a8421c78 adds two product needs:
(a) Campaign links must stop working on their own when the campaign ends (31 December, 23:59 UTC), with no one deactivating links by hand.
(b) Marketing must be able to see how many clicks each link got in each hour during the TV ad slots.

The current service has these defects or gaps for campaign traffic:
(1) SQLite has no hardening: no WAL, the default busy timeout and no pool configuration. Redirect lookups block behind click and create commits, and the runbook describes this behaviour wrongly.
(2) Pool sizing cannot be configured.
(3) HEAD /healthz and HEAD /readyz fall through to the catch-all HEAD /{code} route and return 404, so HEAD-based uptime monitors would report an outage.
(4) Stats load one row per click, so memory and time grow without limit.
(5) Links cannot expire. Redirect resolution checks only is_active.
(6) Analytics are bucketed only by UTC day, so clicks during an ad slot cannot be measured.
(7) There is no migration tooling. create_all never alters existing tables, so adding a column without an upgrade step would make every redirect against the production database return 503.
(8) On MySQL, codes match case-insensitively. This is low priority because production is SQLite.
(9) The docs contradict the code on authentication.

This change does the following:
- Makes the redirect path fast and resistant to contention on one SQLite instance.
- Makes DB tuning configurable.
- Adds a per-link expires_at that is enforced on GET and HEAD, can be set at creation or on existing links, and is applied to deployed databases by an idempotent startup schema upgrade.
- Adds a bounded, API-key-protected hourly click endpoint.
- Makes the probes and analytics trustworthy.
- Corrects the docs and adds a campaign runbook.

Every existing contract must stay intact: a 307 with the exact Location header and no-store; an immediate 410 after deactivation; synchronous click counting; privacy; the unchanged daily stats contract; idempotency replays of keys stored before the deploy; and the architecture rules. The deadline is one week, so the scope favours small, low-risk changes. That means no caching, no asynchronous ingestion, no new drivers, and only one additive, nullable schema change. New ids continue from FR-1..FR-21 and NFR-1..NFR-20.

## Delivered requirements
- **FR-14** HEAD support on liveness and readiness probes — verified by tests
- **FR-15** Redirects resilient to database write contention (hardened SQLite) — verified by tests
- **FR-16** Configurable database connection pool — verified by tests
- **FR-17** Lean redirect query budget — not traced
- **FR-18** Bounded-memory daily stats aggregation — verified by tests
- **FR-19** Case-sensitive short-code matching on MySQL — not traced
- **FR-20** Accurate documentation, campaign runbook and changelog — verified by tests
- **FR-21** Regression protection of existing contracts — verified by tests
- **FR-22** Automatic link expiry enforced on redirect — verified by tests
- **FR-23** Set expiry on link creation and expose it in responses — verified by tests
- **FR-24** Set, change or clear expiry on existing links — verified by tests
- **FR-25** Hourly click counts per link — verified by tests
- **FR-26** Idempotent startup schema upgrade for links.expires_at — verified by tests

## API
- `POST /api/links` — Create a short link. Requires X-API-Key. Optional Idempotency-Key header and optional expires_at.
- `GET /api/links/{code}` — Get link metadata, including deactivated and expired links. Requires X-API-Key.
- `PATCH /api/links/{code}` — Set, change or clear the expiry of an existing link. Requires X-API-Key.
- `DELETE /api/links/{code}` — Deactivate (soft-delete) a link. Idempotent. Requires X-API-Key. Unchanged.
- `GET /api/links/{code}/stats` — Daily click stats. Contract unchanged; now aggregated in SQL. Requires X-API-Key.
- `GET /api/links/{code}/stats/hourly` — Clicks per UTC hour for one link over [start, end), zero-filled. Requires X-API-Key.
- `GET /{code}` — Public redirect. Records exactly one click on success (best-effort). The HEAD /{code} twin (not in the schema) returns the same status and headers and records no click.
- `GET /healthz` — Liveness probe; never touches the DB. HEAD /healthz (not in the schema) returns 200 with an empty body. POST returns 405.
- `GET /readyz` — Readiness probe (SELECT 1). HEAD /readyz (not in the schema) returns 200 with an empty body, or 503. POST returns 405.

## Quality
- Tests: 404 passed, 0 failed
- Coverage: 99.78%
- Security findings: 6 (0 high/critical)

## Architecture decisions
- ADR-1 Harden SQLite with a connect listener, connect_args timeout and QueuePool settings: For file-based SQLite:
- create_engine(url, poolclass=QueuePool, pool_size, max_overflow, pool_timeout, connect_args={'check_same_thread': False, 'timeout': (SQLITE_BUSY_TIMEOUT_MS + 0.5) / 1000.0}).
- An event.listens_for(engine, 'connect') handler executes the literal statements 'PRAGMA journal_mode=WAL' and 'PRAGMA synchronous=NORMAL' on a raw DBAPI cursor.
- The +0.5 ms makes pysqlite's int(timeout*1000) truncation land exactly on the configured value.

In-memory SQLite gets only check_same_thread=False. Non-SQLite gets pool_pre_ping=True plus the pool settings.
- ADR-2 Idempotent startup ALTER instead of migration tooling: After create_all, the lifespan calls app.core.schema.ensure_links_expires_at(engine):
- It uses sqlalchemy.inspect on a fresh connection.
- If 'expires_at' is not among the links columns, it runs the dialect-guarded literal ALTER inside engine.begin().
- It logs 'added' or 'already_present'.
- On any exception it logs an ERROR and re-raises, so startup aborts.
- ADR-3 Expiry evaluated in Python on the narrow lookup row, with an injectable clock: LinkRepository.get_redirect_target selects only id, url, is_active and expires_at. LinkService._check_redirectable(target) raises GoneError() if the link is inactive, then GoneError(LINK_EXPIRED_DETAIL) if expires_at is not None and self._utcnow() >= expires_at. GET and HEAD both go through _resolve_target, which calls _check_redirectable. The clock is LinkService(now=callable|None); _utcnow() calls it, falling back to models.utcnow(), and normalises to naive UTC.
- ADR-4 Backward-compatible idempotency hash: request_hash = sha256(url) when expires_at is None; otherwise sha256(url + '\n' + canonical UTC instant 'YYYY-MM-DDTHH:MM:SS.ffffffZ'). The replay check runs before the future-instant check.
- ADR-5 SQL aggregation with dialect-guarded bucket expressions that use no bound parameters: Daily and hourly stats use SELECT bucket, COUNT(id) ... WHERE link_id = :id AND created_at >= :start [AND created_at < :end] GROUP BY bucket.

Day bucket:
- sqlite: func.date(created_at)
- postgresql (and the fallback): cast(created_at, Date)
- mysql/mariadb: func.date(created_at)

Hour bucket:
- sqlite: func.strftime(literal_column("'%Y-%m-%d %H:00:00'"), created_at)
- postgresql (and the fallback): func.date_trunc(literal_column("'hour'"), created_at)
- mysql/mariadb: func.timestampadd(literal_column('HOUR'), func.hour(created_at), func.date(created_at))

Result values are normalised in Python (str -> date/datetime). Module-level statement builders take dialect_name so tests can compile them for all three dialects.
- ADR-6 Explicit HEAD routes for the probes: health.py adds @router.head('/healthz', include_in_schema=False) and @router.head('/readyz', include_in_schema=False). They return Response(status_code=200) with an empty body. /readyz shares a private _check_database(db) helper with GET.
- ADR-7 End the read transaction before the click insert; best-effort WARNING on failure: In resolve_and_record_click, after the lookup and checks, call self.session.rollback(). This ends the read transaction and is a transaction-boundary call, not a statement. Then call ClickRepository.add(...) and session.commit() inside try/except Exception. On failure:
- Roll back, suppressing any secondary exception.
- Log exactly one logger.warning('failed to record click for link_id=%s (%s)', link_id, type(exc).__name__), with no IP, no exception message and no traceback.
- Return the URL.
- ADR-8 Future-instant validation in the service with a 422 domain error: Add InvalidInputError(AppError) with status 422 in app/core/errors.py. Schemas only parse and normalise (to naive UTC, non-string values rejected). LinkService raises InvalidInputError('expires_at must be in the future') using its injectable clock. StatsService raises InvalidInputError for invalid hourly ranges.
- ADR-9 No-store on redirect-path error responses: redirect.py catches NotFoundError and GoneError from the service and returns JSONResponse({'detail': exc.detail}, status_code=exc.status_code, headers={'Cache-Control': 'no-store'}). Other exceptions propagate to the global handlers (SQLAlchemyError -> 503).
- ADR-10 MySQL case-sensitive codes via a type variant: Declare code as String(32).with_variant(String(32, collation='utf8mb4_bin'), 'mysql', 'mariadb'). Document the manual DDL for existing MySQL databases: ALTER TABLE links MODIFY code VARCHAR(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL;

## Known risks
- The startup upgrade is not applied or is wrong, so every links query fails on the production DB (503 on every redirect). — mitigation: Idempotent ensure_links_expires_at in the lifespan, which fails startup on error. Test against an old-schema SQLite file with existing rows and idempotency keys. Back up before deploying (runbook). Document the manual SQL.
- Pool kwargs passed for in-memory SQLite raise at create_app('sqlite://'). — mitigation: The in-memory branch passes only check_same_thread. Tests cover 'sqlite://' and ':memory:'.
- SQL bucketing differs from the old Python bucketing (window edges, ordering, return types). — mitigation: Keep the >= since predicate and sort in Python. Normalise str to date/datetime. The unmodified tests/test_stats.py is the oracle, plus hourly boundary tests and compile tests for three dialects.
- A naive vs aware comparison, or datetime overflow (year 9999 with an offset, +1 hour at datetime.max), raises a 500. — mitigation: Everything is normalised to naive UTC at the schema and service boundaries. OverflowError is converted to ValueError (schemas) or InvalidInputError (services). Tests use +01:00, -05:00 and +05:30 and extreme values.
- '+' in hourly query strings decodes as a space, so the offset is misread and the request returns 422. — mitigation: Document URL-encoding (%2B) or using 'Z' in API.md and the runbook. A parse failure is a clean 422, never a wrong answer.
- Click drops under bursts because of the short busy timeout recommended for the campaign. — mitigation: Every drop logs one WARNING (countable per hour). Default 5000 ms, runbook value 1000 ms. NFR-19 burst test.
- Operators enter the wrong expiry instant or year, or miss campaign links. — mitigation: Runbook PATCH loop over the code list, verified with GET detail (expires_at, is_expired), as a pre-launch checklist item. PATCH allows correction.
- The WAL sidecar files break naive backups or network filesystems. — mitigation: OPERATIONS.md: use sqlite3 .backup or copy all three files while the service is stopped; local writable directory only. The startup line shows the effective journal_mode.
- Overlapping concurrent work packages edit shared files. — mitigation: Strict per-file ownership. Interfaces (names and signatures) are fixed in this design.
