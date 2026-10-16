# Release v1.1.0 — CR-142 (custom aliases and link expiration) and BUG-77 (URL scheme validation investigation) for the URL shortener

The existing FastAPI/SQLAlchemy URL shortener only issues random 7-character base62 codes, and its links never expire. CR-142 adds two things. First, clients can choose their own short code (alias): 3-32 characters from [A-Za-z0-9_-]. Reserved words give 422 and a taken alias gives 409. Second, links can have an optional future expiry `expires_at`. Redirecting an expired link gives 410 Gone, and link metadata reports whether the link is expired. Both features must be backwards compatible. Existing endpoints, response fields and the 165 existing tests must keep working. The new `links.expires_at` column needs an explicit migration, because startup only runs `create_all`, which never alters existing tables. BUG-77 says upper-case schemes (`HTTP://Example.com`) were rejected and that a `javascript:` URL was once accepted. Investigation shows the current validator accepts upper-case schemes and rejects every `javascript:` variant, so the URL validator has no defect. Regression tests must still lock in the reported inputs. Other defects found during the investigation must be fixed: `metrics` is missing from the reserved list, the lookup format check is base62-only, the idempotency hash covers only the URL, every IntegrityError is treated as a code collision, and the docs wrongly say there is no authentication.

## Delivered requirements
- **FR-1** Create a link with a custom alias — verified by tests
- **FR-2** Reject reserved words as aliases — verified by tests
- **FR-3** Alias uniqueness gives 409 — verified by tests
- **FR-4** Alias links resolve on every code-addressed endpoint — verified by tests
- **FR-5** Accept and validate an optional expires_at at creation — verified by tests
- **FR-6** Expired links return 410 Gone on redirect — verified by tests
- **FR-7** Link metadata reports expiry state — verified by tests
- **FR-8** Explicit, idempotent schema migration for links.expires_at — verified by tests
- **FR-9** Idempotency-Key covers alias and expires_at — verified by tests
- **FR-10** Add 'metrics' to the reserved code list — verified by tests
- **FR-11** BUG-77 regression tests for URL scheme validation — verified by tests
- **FR-12** Backwards compatibility and regression protection — verified by tests
- **FR-13** OpenAPI response documentation for new cases — not traced
- **FR-14** Documentation updates, including the stale authentication statements — not traced

## API
- `POST /api/links` — Create a short link, optionally with a custom alias and/or an expiry. Requires X-API-Key. Optional header Idempotency-Key (1-255 chars, not blank). Sets Location: /api/links/{code}. A replay sets Idempotent-Replayed: true and returns the original link's current state.
- `GET /api/links/{code}` — Get link metadata (active, deactivated or expired). Requires X-API-Key. {code} accepts ^[A-Za-z0-9_-]{1,32}$. Anything else gives 404 without a DB lookup.
- `DELETE /api/links/{code}` — Soft-delete (deactivate) a link. Idempotent. Works for expired links and aliases containing '-'/'_'. Requires X-API-Key.
- `GET /api/links/{code}/stats` — Click statistics. Works for deactivated and expired links and aliases containing '-'/'_'. Requires X-API-Key.
- `GET /{code}` — Public redirect (catch-all router, registered last). 307 with the exact stored URL in Location (not re-quoted) and Cache-Control: no-store. Records a click best-effort. HEAD /{code} behaves the same but records no click, and is now included in OpenAPI with the same 307/404/410 responses. 404/410 send no Location and record no click.
- `GET /healthz` — Liveness probe (unchanged, not shadowed by /{code})
- `GET /readyz` — Readiness probe (unchanged, not shadowed by /{code})

## Quality
- Tests: 338 passed, 0 failed
- Coverage: 99.06%
- Security findings: 6 (0 high/critical)

## Architecture decisions
- ADR-1 Alias format in schema, reserved words in service with codes.RESERVED_CODES: LinkCreate validates alias type, length and charset (StrictStr, 3-32, [A-Za-z0-9_-]), giving a FastAPI 422 list detail. LinkService rejects reserved aliases case-insensitively via codes.is_reserved, raising UnprocessableEntityError('Alias is reserved'), which gives 422. 'metrics' is added to codes.RESERVED_CODES, so generated codes and aliases share one list.
- ADR-2 Separate lookup validator instead of widening CODE_PATTERN: Add codes.is_valid_lookup_code (^[A-Za-z0-9_-]{1,32}$) and use it in LinkService._get_or_404 and StatsService.get_stats. is_valid_code_format stays base62-only.
- ADR-3 Alias path is a single insert, and IntegrityError gives 409: If alias is given: pre-check get_by_code (friendly 409), then one LinkRepository.add. On IntegrityError: rollback. If an Idempotency-Key was sent and its record now exists, replay it or return 409. Otherwise raise ConflictError('Alias already in use'). codes.generate_code is never called on this path. The generated-code retry loop is unchanged.
- ADR-4 Idempotent startup migration with literal DDL in core/db.ensure_schema: After create_all, ensure_schema(engine) inspects links. If expires_at is missing, it executes text('ALTER TABLE links ADD COLUMN expires_at DATETIME NULL'). On the postgresql dialect only, it executes text('ALTER TABLE links ADD COLUMN expires_at TIMESTAMP NULL'), because PostgreSQL has no DATETIME type. Both are literal strings passed straight to text(). If the ALTER fails with DBAPIError and the column now exists, this is a tolerated concurrent race. Otherwise it logs an error and re-raises. The manual SQL is documented. No Alembic.
- ADR-5 expires_at: ISO-only parsing in schema, future check in service with an injectable clock: LinkCreate.expires_at: dt.datetime|None with a mode='before' validator that accepts only None, datetime instances, or str matching a strict ISO-8601 date-time regex (rejects int/bool/''/'tomorrow'/date-only/unix strings). Pydantic then parses it (rejecting month 13), and a mode='after' validator normalises it with _as_utc (naive = UTC). LinkService converts to naive UTC and requires expires_at > self.now(). Otherwise it raises UnprocessableEntityError('expires_at must be in the future'). The clock is LinkService(clock=...), which deps fill from app.state.clock (None means models.utcnow, looked up at call time).
- ADR-6 Backwards-compatible idempotency request_hash: compute_request_hash(url, alias, expires_at_naive_utc) returns sha256(url) when alias and expires_at are both None. Otherwise it returns sha256 of json.dumps({'alias':..., 'expires_at': 'YYYY-MM-DDTHH:MM:SS.ffffffZ' or None, 'url':...}, sort_keys=True, separators=(',',':'), ensure_ascii=False). The idempotency lookup runs before the reserved/future checks, so a retried successful request always replays.
- ADR-7 Expiry evaluated from the loaded row; deactivation takes precedence: Link.is_expired_at(now) returns expires_at is not None and expires_at <= now. Redirect and HEAD check `not is_active` first (GoneError, 'Link has been deactivated'), then expiry (LinkExpiredError, 'Link has expired'), before any click insert. is_expired for responses is computed by the service and returned in CreateResult/LinkDetail. is_active is not changed by expiry.
- ADR-8 Expose HEAD /{code} in OpenAPI: Change @router.head('/{code}') to include_in_schema=True with responses 307/404/410, so FR-13's 410 documentation covers HEAD.

## Known risks
- Legacy databases lack links.expires_at. Without migration every links query gives 503. — mitigation: ensure_schema runs on every startup after create_all. Legacy-DB boot test run twice (NFR-3). Manual SQL and 'migrate once before multi-instance rollout' in OPERATIONS.md.
- A request_hash change breaks replay of stored keys. — mitigation: Keep sha256(url) when there are no new fields (ADR-6). Test with a directly inserted legacy row.
- Postgres rejects DATETIME, so the required literal DDL alone would break legacy Postgres DBs. — mitigation: Literal TIMESTAMP branch for the postgresql dialect only (ADR-4). Documented in OPERATIONS.md with per-dialect manual SQL.
- Alias race mapped to a retry or 503 instead of 409. — mitigation: Separate alias branch (ADR-3). Monkeypatched IntegrityError test asserts 409, rollback, zero extra rows, and that generate_code is not called.
- Naive/aware datetime mixing causes a TypeError (500) or shifted expiry. — mitigation: Schema normalises to aware UTC. The service converts to naive UTC before compare/persist. now() normalises any aware injected clock. Offset and naive tests.
- Lax parsing accepts numeric expires_at. — mitigation: Before-validator ISO regex (ADR-5). Tests for 1893456000, true, '', 'tomorrow', month 13.
- MySQL case-insensitive collation makes 'MyLink'/'mylink' collide and lookups case-insensitive. — mitigation: Documented caveat. Binary collation deferred. Tests use SQLite.
- Pydantic constraint handling on Optional StrictStr could let bad aliases through. — mitigation: Belt and braces: an after-validator re.fullmatch(r'[A-Za-z0-9_-]{3,32}') plus a defensive codes.is_valid_alias check in the service (422). Tests cover every listed bad input.
- Computed is_expired makes replay bodies change over time. — mitigation: Documented: replays reflect current state, as is_active already does. Computed from the loaded row.
- Widened lookup format changes behaviour for existing malformed-code tests ('abc-def', 'abc_def'). — mitigation: These become valid-but-unknown and still return 404, which the existing assertions accept (404/422 and 404 respectively).
