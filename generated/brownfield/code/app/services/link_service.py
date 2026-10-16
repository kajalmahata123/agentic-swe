"""Link lifecycle business rules: creation, lookup, deactivation and redirects.

The service owns transaction boundaries: repositories only flush, and this module
alone calls ``commit()``/``rollback()``.

Creation has two paths. With a client-chosen ``alias`` the link is inserted exactly
once and a uniqueness violation maps to 409; without one, a random base62 code is
generated with a bounded retry loop. Alias and ``expires_at`` values are never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    BadRequestError,
    ConflictError,
    GoneError,
    LinkExpiredError,
    NotFoundError,
    ServiceUnavailableError,
    UnprocessableEntityError,
)
from app.models import ClickEvent, IdempotencyKey, Link, utcnow
from app.repositories import ClickRepository, IdempotencyRepository, LinkRepository
from app.services import codes

logger = logging.getLogger("app.services.links")

MAX_REFERRER_LEN = 2048
MAX_USER_AGENT_LEN = 512
MAX_IDEMPOTENCY_KEY_LEN = 255

ALIAS_TAKEN_DETAIL = "Alias already in use"
ALIAS_RESERVED_DETAIL = "Alias is reserved"
ALIAS_INVALID_DETAIL = "Invalid alias"
EXPIRES_NOT_FUTURE_DETAIL = "expires_at must be in the future"


@dataclass
class CreateResult:
    """Outcome of ``create_link``.

    ``replayed`` is True for an idempotent replay; ``is_expired`` reflects the link's
    expiry state at the time the result was produced.
    """

    link: Link
    replayed: bool
    is_expired: bool = False


@dataclass
class LinkDetail:
    """A link (active, deactivated or expired) with its all-time click count."""

    link: Link
    total_clicks: int
    is_expired: bool = False


def hash_ip(ip: str | None, salt: str | None) -> str | None:
    """Return the hex HMAC-SHA256 of ``ip`` keyed by ``salt``, or None if either is missing."""
    if not ip or not salt:
        return None
    return hmac.new(salt.encode(), ip.encode(), hashlib.sha256).hexdigest()


def _to_naive_utc(value: datetime | None) -> datetime | None:
    """Convert an aware datetime to naive UTC; naive values (already UTC) pass through."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def compute_request_hash(url: str, alias: str | None, expires_at: datetime | None) -> str:
    """Return the idempotency fingerprint of a creation payload (hex SHA-256).

    When neither ``alias`` nor ``expires_at`` is given the hash is ``sha256(url)``, which
    is what was stored before CR-142, so existing keys keep replaying. Otherwise it is
    the SHA-256 of a canonical JSON document of url, alias and the UTC-normalised expiry,
    so equal instants expressed in different offsets produce the same hash.
    """
    if alias is None and expires_at is None:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()
    exp = _to_naive_utc(expires_at)
    payload = json.dumps(
        {
            "alias": alias,
            "expires_at": exp.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if exp else None,
            "url": url,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LinkService:
    """Business operations on short links, bound to one database session."""

    def __init__(
        self,
        session: Session,
        *,
        code_length: int = 7,
        max_attempts: int = 5,
        ip_hash_salt: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session
        self.code_length = code_length
        self.max_attempts = max_attempts
        self.ip_hash_salt = ip_hash_salt
        self._clock = clock
        self.link_repo = LinkRepository(session)
        self.click_repo = ClickRepository(session)
        self.idem_repo = IdempotencyRepository(session)

    def now(self) -> datetime:
        """Return the current time as naive UTC, from the injected clock if one is set."""
        value = self._clock() if self._clock is not None else utcnow()
        if value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value

    def create_link(
        self,
        url: str,
        idempotency_key: str | None = None,
        *,
        alias: str | None = None,
        expires_at: datetime | None = None,
    ) -> CreateResult:
        """Create a new active link, honouring an optional Idempotency-Key.

        With ``alias`` the link is stored with ``code == alias`` (case preserved) in a
        single insert; otherwise a random code is generated. ``expires_at`` (aware or
        naive UTC) is persisted as naive UTC and must lie strictly in the future.

        Raises:
            BadRequestError: the key is blank or longer than 255 characters.
            ConflictError: the key was already used with a different payload, or the
                alias is already taken (including by a concurrent request).
            UnprocessableEntityError: the alias is malformed or reserved, or
                ``expires_at`` is not in the future.
            ServiceUnavailableError: no unique generated code could be allocated.
        """
        key = idempotency_key
        if key is not None and (key.strip() == "" or len(key) > MAX_IDEMPOTENCY_KEY_LEN):
            raise BadRequestError("Invalid Idempotency-Key header")

        exp = _to_naive_utc(expires_at)
        request_hash = compute_request_hash(url, alias, exp)

        if key is not None:
            existing = self.idem_repo.get_by_key(key)
            if existing is not None:
                return self._replay_or_conflict(existing, request_hash)

        if alias is not None:
            if not codes.is_valid_alias(alias):
                raise UnprocessableEntityError(ALIAS_INVALID_DETAIL)
            if codes.is_reserved(alias):
                raise UnprocessableEntityError(ALIAS_RESERVED_DETAIL)

        if exp is not None and exp <= self.now():
            raise UnprocessableEntityError(EXPIRES_NOT_FUTURE_DETAIL)

        if alias is not None:
            return self._create_with_alias(url, alias, exp, key, request_hash)

        for _attempt in range(self.max_attempts):
            code = codes.generate_code(self.code_length)
            if codes.is_reserved(code):
                continue

            link = Link(code=code, url=url, is_active=True, expires_at=exp)
            try:
                self.link_repo.add(link)
            except IntegrityError:
                self.session.rollback()
                continue

            return self._finish_create(link, key, request_hash)

        logger.error("short code allocation exhausted")
        raise ServiceUnavailableError("Could not allocate a unique short code, please retry")

    def _create_with_alias(
        self,
        url: str,
        alias: str,
        exp: datetime | None,
        key: str | None,
        request_hash: str,
    ) -> CreateResult:
        """Insert a link whose code is ``alias``; any uniqueness violation is a 409.

        Never generates a random code. If a concurrent request with the same
        Idempotency-Key won the race, its result is replayed (or 409 on a payload
        mismatch).
        """
        if self.link_repo.get_by_code(alias) is not None:
            raise ConflictError(ALIAS_TAKEN_DETAIL)

        link = Link(code=alias, url=url, is_active=True, expires_at=exp)
        try:
            self.link_repo.add(link)
        except IntegrityError:
            self.session.rollback()
            if key is not None and (winner := self.idem_repo.get_by_key(key)) is not None:
                return self._replay_or_conflict(winner, request_hash)
            raise ConflictError(ALIAS_TAKEN_DETAIL) from None

        return self._finish_create(link, key, request_hash)

    def _finish_create(
        self, link: Link, key: str | None, request_hash: str
    ) -> CreateResult:
        """Record the Idempotency-Key (if any) for the flushed ``link`` and commit.

        The key row is only ever committed together with the link, so a failed attempt
        never leaves an idempotency record behind.
        """
        if key is not None:
            try:
                self.idem_repo.add(
                    IdempotencyKey(key=key, request_hash=request_hash, link_id=link.id)
                )
            except IntegrityError:
                self.session.rollback()
                winner = self.idem_repo.get_by_key(key)
                if winner is None:
                    raise ConflictError("Idempotency-Key conflict, please retry") from None
                return self._replay_or_conflict(winner, request_hash)

        is_expired = link.is_expired_at(self.now())
        self.session.commit()
        return CreateResult(link=link, replayed=False, is_expired=is_expired)

    def _replay_or_conflict(self, record: IdempotencyKey, request_hash: str) -> CreateResult:
        """Replay the link stored for ``record`` or raise if the payload differs."""
        if record.request_hash != request_hash:
            raise ConflictError("Idempotency-Key already used with a different payload")
        link = self.session.get(Link, record.link_id)
        if link is None:
            raise ConflictError("Idempotency-Key conflict, please retry")
        return CreateResult(link=link, replayed=True, is_expired=link.is_expired_at(self.now()))

    def _get_or_404(self, code: str) -> Link:
        """Return the link for ``code`` or raise ``NotFoundError`` (malformed codes never query)."""
        if not codes.is_valid_lookup_code(code):
            raise NotFoundError()
        link = self.link_repo.get_by_code(code)
        if link is None:
            raise NotFoundError()
        return link

    def get_link_detail(self, code: str) -> LinkDetail:
        """Return a link (active, deactivated or expired), its click count and expiry state."""
        link = self._get_or_404(code)
        return LinkDetail(
            link=link,
            total_clicks=self.click_repo.count_for_link(link.id),
            is_expired=link.is_expired_at(self.now()),
        )

    def deactivate(self, code: str) -> None:
        """Soft-delete a link. Idempotent: an inactive link keeps its deactivated_at."""
        link = self._get_or_404(code)
        if link.is_active:
            link.is_active = False
            link.deactivated_at = utcnow()
            self.session.commit()

    def _get_redirectable(self, code: str) -> Link:
        """Return a link that may be redirected to, using only the already-loaded row.

        Raises:
            NotFoundError: the code is malformed or unknown.
            GoneError: the link is deactivated (takes precedence over expiry).
            LinkExpiredError: the link's ``expires_at`` is at or before now.
        """
        link = self._get_or_404(code)
        if not link.is_active:
            raise GoneError()
        if link.is_expired_at(self.now()):
            raise LinkExpiredError()
        return link

    def resolve_for_redirect(self, code: str) -> str:
        """Return the target URL of an active, unexpired link without recording a click (HEAD)."""
        return self._get_redirectable(code).url

    def resolve_and_record_click(
        self,
        code: str,
        referrer: str | None,
        user_agent: str | None,
        client_ip: str | None,
    ) -> str:
        """Return the target URL of an active, unexpired link and record a click (best-effort).

        A failure while recording the click is rolled back and logged (never with the
        client IP); the URL is still returned so the redirect succeeds.
        """
        link = self._get_redirectable(code)
        url = link.url
        link_id = link.id
        try:
            self.click_repo.add(
                ClickEvent(
                    link_id=link_id,
                    referrer=referrer[:MAX_REFERRER_LEN] if referrer else None,
                    user_agent=user_agent[:MAX_USER_AGENT_LEN] if user_agent else None,
                    ip_hash=hash_ip(client_ip, self.ip_hash_salt),
                )
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            logger.exception("failed to record click for link_id=%s", link_id)
        return url
