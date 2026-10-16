# Release v1.0.0 — Production-ready URL Shortener HTTP Service

Build a greenfield HTTP service (Python 3.11, FastAPI, SQLAlchemy 2.0, Pydantic v2) where clients create short links for long http/https URLs. Visiting a short link redirects to the original URL with HTTP 307. Clients can look up and soft-delete links. Every redirect is recorded as a privacy-preserving click event (timestamp, referrer, user agent; never the raw IP), and per-link stats are available (total clicks, clicks per UTC day, top referrers). The service must hold up in production: strict URL validation, random collision-safe short codes backed by a DB unique constraint, idempotent creation through an Idempotency-Key header, liveness/readiness probes, and consistent JSON errors that never leak stack traces. Persistence is relational through SQLAlchemy: SQLite by default, configurable via DATABASE_URL. The workspace is empty apart from .gitignore, so there is no existing code to protect.

## Delivered requirements
- **FR-1** Create short link — verified by tests
- **FR-2** URL validation — verified by tests
- **FR-3** Random, URL-safe, collision-safe short codes — verified by tests
- **FR-4** Idempotent creation via Idempotency-Key — verified by tests
- **FR-5** Redirect to original URL — verified by tests
- **FR-6** Look up link metadata — verified by tests
- **FR-7** Deactivate (soft-delete) link — verified by tests
- **FR-8** Record click events — verified by tests
- **FR-9** Per-link stats — verified by tests
- **FR-10** Liveness and readiness probes — verified by tests
- **FR-11** Consistent JSON error responses — verified by tests
- **FR-12** Configuration and persistence — verified by tests
- **FR-13** OpenAPI documentation — not traced

## API
- `POST /api/links` — Create a short link. An optional Idempotency-Key header makes the request idempotent.
- `GET /api/links/{code}` — Get link metadata, including for deactivated links.
- `DELETE /api/links/{code}` — Soft-delete (deactivate) a link. Idempotent.
- `GET /api/links/{code}/stats` — Aggregated click stats for a link (active or deactivated).
- `GET /{code}` — Redirect to the original URL and record a click event. (HEAD /{code} is also implemented with the same status and Location, without recording a click, and is hidden from the schema.)
- `GET /healthz` — Liveness probe. Does not touch the database.
- `GET /readyz` — Readiness probe. Runs SELECT 1 through the get_db session.

## Quality
- Tests: 165 passed, 0 failed
- Coverage: 99.19%
- Security findings: 10 (0 high/critical)

## Accepted policy exceptions
- CMP-001: client IP must be pseudonymised before storage (privacy / GDPR) (app/api/redirect.py) (by human:kajalmahata: IP is HMAC-SHA256 pseudonymised in services/link_service.hash_ip before storage (NULL without salt); router only forwards it.)

## Architecture decisions
- ADR-1 Strict layering with services owning transactions: Routers in app/api depend only on services, schemas and core. Services accept a SQLAlchemy Session plus plain config values, use repositories, and alone call commit()/rollback(). Repositories only add/flush/query. Models import only sqlalchemy. Domain exceptions live in app/core/errors.py, which keeps services free of FastAPI imports.
- ADR-2 Random base62 codes backed by a DB unique constraint and retry: codes.generate_code(length) uses secrets.choice over [A-Za-z0-9]. The service inserts and flushes. On IntegrityError it rolls back and retries with a new code, up to CODE_MAX_ATTEMPTS (default 5). A reserved candidate (compared case-insensitively against RESERVED_CODES) uses up an attempt and is regenerated. When attempts run out the service raises a 503.
- ADR-3 Idempotency via a table with a unique key and request hash, written in the link's transaction: If the key exists: a matching request_hash replays the stored link (201, Idempotent-Replayed: true), and a different hash returns 409. Otherwise the link is inserted and flushed, then the key row is inserted and flushed in the same transaction, then the transaction commits. An IntegrityError on the key flush (a concurrent winner) rolls back both rows, after which the service re-reads the key and replays or returns 409. Keys are global and never expire (Q-3). Replays are rebuilt from the stored link row.
- ADR-4 App factory with a per-app engine on app.state: create_app(settings=None) builds Settings() when none are given, creates the engine and sessionmaker, and stores settings, engine and session_factory on app.state. get_db(request) and get_settings(request) read from app.state. Tables are created by Base.metadata.create_all(engine) in the lifespan startup, and the engine is disposed on shutdown.
- ADR-5 Naive-UTC timestamps and Python-side day bucketing: All DateTime columns are portable naive DateTime values holding UTC (models.base.utcnow() = datetime.now(timezone.utc).replace(tzinfo=None)). Response schemas convert to tz-aware UTC on output. clicks_per_day fetches only created_at for rows in the window and groups by .date() in Python. top_referrers uses SQL GROUP BY referrer, followed by a deterministic Python sort and a cut to 10.
- ADR-6 Raw 307 Response for redirects; ASCII-only target URLs: The redirect handler returns Response(status_code=307, headers={'Location': url, 'Cache-Control': 'no-store'}) instead of starlette's RedirectResponse, which percent-quotes the URL. URL validation additionally rejects non-ASCII characters (clients must send punycode or percent-encoded URLs).
- ADR-7 Centralized JSON error handling with a middleware catch-all: AppError subclasses carry status_code and detail: BadRequestError 400, NotFoundError 404, ConflictError 409, GoneError 410, ServiceUnavailableError 503. Handlers are registered for AppError, StarletteHTTPException (404/405, keeping headers such as Allow), RequestValidationError (422), SQLAlchemyError (503 'Service temporarily unavailable', logged), and Exception (500). RequestLoggingMiddleware also wraps call_next in try/except: an exception is logged with its traceback and answered with 500 {"detail": "Internal Server Error"}.
- ADR-8 Synchronous, best-effort click recording: After resolving an active link, the service inserts a ClickEvent and commits in its own small transaction. Any exception rolls back and is logged (logger.exception, without the IP), and the 307 is still returned. HEAD requests never record clicks.
- ADR-9 No authentication in this iteration: The /api/links router has no auth dependency, but is structured so that a future Depends(require_api_key) can be added at router level without changing the contract.
- ADR-10 Readiness via the shared get_db dependency: /readyz declares db: Session = Depends(get_db) and runs db.execute(text('SELECT 1')) inside try/except Exception, raising ServiceUnavailableError('Database is not ready') on failure. If an overridden get_db itself raises a SQLAlchemyError, the global SQLAlchemyError handler returns 503 too.

## Known risks
- Without authentication anyone who knows a code can deactivate a link or read its stats (Q-1). — mitigation: Codes are high-entropy. The router is ready for an API-key dependency. Escalate before public launch.
- The catch-all GET /{code} could shadow fixed routes, or test-added routes registered after it. — mitigation: Register the redirect router last in create_app. The reserved-word list is applied in code generation. Tests that add ad-hoc routes must use multi-segment paths such as /_test/boom, which /{code} cannot match.
- SQLite 'database is locked' errors under concurrent writes. — mitigation: check_same_thread=False, short transactions, best-effort click recording, SQLAlchemyError mapped to 503. Recommend Postgres via DATABASE_URL for production.
- uvicorn's access log prints client IPs, which would violate NFR-1. — mitigation: Run with `uvicorn app.main:app --no-access-log`. The app's own request log contains method, path (no query string), status and duration_ms only.
- Python-side day bucketing loads every created_at in the window for very popular links. — mitigation: Only one column is selected and the window is capped at 365 days. The (link_id, created_at) index serves the query. A future iteration can add pre-aggregated daily counters.
- The idempotency race path is hard to exercise. — mitigation: The service re-reads the key after an IntegrityError and returns 409 if it is still missing. Tests simulate the race by pre-inserting the key row after the first lookup (monkeypatch IdempotencyRepository.get_by_key to return None once).
- Datetime serialization differences between the first response and a replay would break FR-4's identical-body check. — mitigation: Both responses are built from DB-loaded attributes (expire_on_commit=True reloads after commit). Schemas normalize datetimes to tz-aware UTC.
