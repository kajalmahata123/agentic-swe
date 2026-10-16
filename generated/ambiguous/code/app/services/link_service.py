"""Link lifecycle business rules: creation, expiry, lookup, deactivation and redirects.

The service owns transaction boundaries: repositories only flush, and this module
alone calls ``commit()``/``rollback()``.

Expiry rule: a link is expired when ``now (UTC) >= expires_at``. The clock is
injectable (``LinkService(now=...)``) so the rule can be tested deterministically.
GET and HEAD redirects share one private helper that checks deactivation first
(410 "Link has been deactivated") and then expiry (410 "Link has expired"), using
the narrow redirect projection so the check adds no SQL statement. Click recording
is synchronous and best-effort: a failed INSERT/COMMIT is rolled back and logged
once (without the client IP), and the redirect still succeeds.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    LINK_EXPIRED_DETAIL,
    BadRequestError,
    ConflictError,
    GoneError,
    InvalidInputError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.models import ClickEvent, IdempotencyKey, Link, to_naive_utc, utcnow
from app.repositories import (
    ClickRepository,
    IdempotencyRepository,
    LinkRepository,
    RedirectTarget,
)
from app.services import codes

logger = logging.getLogger("app.services.links")

MAX_REFERRER_LEN = 2048
MAX_USER_AGENT_LEN = 512
MAX_IDEMPOTENCY_KEY_LEN = 255
EXPIRES_AT_NOT_FUTURE_DETAIL = "expires_at must be in the future"
_HASH_INSTANT_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


@dataclass
class CreateResult:
    """Outcome of ``create_link``; ``replayed`` is True for an idempotent replay."""

    link: Link
    replayed: bool


@dataclass
class LinkDetail:
    """A link (active or not) with its all-time click count and expiry state."""

    link: Link
    total_clicks: int
    is_expired: bool = False


def hash_ip(ip: str | None, salt: str | None) -> str | None:
    """Return the hex HMAC-SHA256 of ``ip`` keyed by ``salt``, or None if either is missing."""
    if not ip or not salt:
        return None
    return hmac.new(salt.encode(), ip.encode(), hashlib.sha256).hexdigest()


class LinkService:
    """Business operations on short links, bound to one database session."""

    def __init__(
        self,
        session: Session,
        *,
        code_length: int = 7,
        max_attempts: int = 5,
        ip_hash_salt: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session
        self.code_length = code_length
        self.max_attempts = max_attempts
        self.ip_hash_salt = ip_hash_salt
        self._clock = now
        self.link_repo = LinkRepository(session)
        self.click_repo = ClickRepository(session)
        self.idem_repo = IdempotencyRepository(session)

    # ------------------------------------------------------------------ clock / expiry

    def _utcnow(self) -> datetime:
        """Return the current instant as naive UTC from the injected clock or ``utcnow``."""
        value = self._clock() if self._clock is not None else utcnow()
        return to_naive_utc(value)

    def _is_expired(self, expires_at: datetime | None) -> bool:
        """Return True iff ``expires_at`` is set and ``now >= expires_at``."""
        return expires_at is not None and self._utcnow() >= to_naive_utc(expires_at)

    def _require_future(self, expires_at: datetime) -> None:
        """Raise ``InvalidInputError`` unless ``expires_at`` (naive UTC) is after now."""
        if expires_at <= self._utcnow():
            raise InvalidInputError(EXPIRES_AT_NOT_FUTURE_DETAIL)

    # ------------------------------------------------------------------ creation

    def create_link(
        self,
        url: str,
        idempotency_key: str | None = None,
        expires_at: datetime | None = None,
    ) -> CreateResult:
        """Create a new active link, honouring an optional Idempotency-Key and expiry.

        ``expires_at`` is normalised to naive UTC. An idempotent replay returns the
        stored link without re-checking that ``expires_at`` lies in the future.

        Raises:
            BadRequestError: the key is blank or longer than 255 characters.
            ConflictError: the key was already used with a different payload.
            InvalidInputError: ``expires_at`` is at or before the current time.
            ServiceUnavailableError: no unique code could be allocated.
        """
        key = idempotency_key
        if key is not None and (key.strip() == "" or len(key) > MAX_IDEMPOTENCY_KEY_LEN):
            raise BadRequestError("Invalid Idempotency-Key header")

        if expires_at is not None:
            expires_at = to_naive_utc(expires_at)

        request_hash = self._request_hash(url, expires_at)

        if key is not None:
            existing = self.idem_repo.get_by_key(key)
            if existing is not None:
                return self._replay_or_conflict(existing, request_hash)

        if expires_at is not None:
            self._require_future(expires_at)

        for _attempt in range(self.max_attempts):
            code = codes.generate_code(self.code_length)
            if codes.is_reserved(code):
                continue

            link = Link(code=code, url=url, is_active=True, expires_at=expires_at)
            try:
                self.link_repo.add(link)
            except IntegrityError:
                self.session.rollback()
                continue

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

            self.session.commit()
            return CreateResult(link=link, replayed=False)

        logger.error("short code allocation exhausted")
        raise ServiceUnavailableError("Could not allocate a unique short code, please retry")

    @staticmethod
    def _request_hash(url: str, expires_at: datetime | None) -> str:
        """Return the 64-hex idempotency fingerprint of a create payload.

        Without ``expires_at`` this is ``sha256(url)``, identical to hashes stored
        before expiry existed, so those keys keep replaying. With ``expires_at``
        (naive UTC) the canonical instant is appended, so the same instant written
        with different offsets hashes identically.
        """
        if expires_at is None:
            return hashlib.sha256(url.encode("utf-8")).hexdigest()
        payload = url + "\n" + expires_at.strftime(_HASH_INSTANT_FORMAT)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _replay_or_conflict(self, record: IdempotencyKey, request_hash: str) -> CreateResult:
        """Replay the link stored for ``record`` or raise if the payload differs."""
        if record.request_hash != request_hash:
            raise ConflictError("Idempotency-Key already used with a different payload")
        link = self.session.get(Link, record.link_id)
        if link is None:
            raise ConflictError("Idempotency-Key conflict, please retry")
        return CreateResult(link=link, replayed=True)

    # ------------------------------------------------------------------ management

    def _get_or_404(self, code: str) -> Link:
        """Return the link for ``code`` or raise ``NotFoundError`` (malformed codes never query)."""
        if not codes.is_valid_code_format(code):
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
            is_expired=self._is_expired(link.expires_at),
        )

    def set_expiry(self, code: str, expires_at: datetime | None) -> LinkDetail:
        """Set (future instant) or clear (``None``) the expiry of an existing link.

        Only ``expires_at`` changes: the link is never reactivated, and its url,
        code, active state, timestamps and idempotency records are untouched.

        Raises:
            NotFoundError: the code is malformed or no such link exists.
            InvalidInputError: ``expires_at`` is at or before the current time.
        """
        link = self._get_or_404(code)
        if expires_at is not None:
            expires_at = to_naive_utc(expires_at)
            self._require_future(expires_at)
        link.expires_at = expires_at
        self.session.commit()
        return self.get_link_detail(code)

    def deactivate(self, code: str) -> None:
        """Soft-delete a link. Idempotent: an inactive link keeps its deactivated_at."""
        link = self._get_or_404(code)
        if link.is_active:
            link.is_active = False
            link.deactivated_at = self._utcnow()
            self.session.commit()

    # ------------------------------------------------------------------ redirects

    def _check_redirectable(self, target: RedirectTarget) -> None:
        """Raise ``GoneError`` if the link is deactivated (precedence) or expired."""
        if not target.is_active:
            raise GoneError()
        if self._is_expired(target.expires_at):
            raise GoneError(LINK_EXPIRED_DETAIL)

    def _resolve_target(self, code: str) -> RedirectTarget:
        """Look up ``code`` with one narrow SELECT and verify it may be redirected.

        Malformed codes raise ``NotFoundError`` without issuing any SQL.
        """
        if not codes.is_valid_code_format(code):
            raise NotFoundError()
        target = self.link_repo.get_redirect_target(code)
        if target is None:
            raise NotFoundError()
        self._check_redirectable(target)
        return target

    def resolve_for_redirect(self, code: str) -> str:
        """Return the target URL of a redirectable link without recording a click (HEAD)."""
        return self._resolve_target(code).url

    def resolve_and_record_click(
        self,
        code: str,
        referrer: str | None,
        user_agent: str | None,
        client_ip: str | None,
    ) -> str:
        """Return the target URL of a redirectable link and record a click (best-effort).

        A failure while recording the click is rolled back and logged once as a
        WARNING (never with the client IP or the exception message); the URL is
        still returned so the redirect succeeds.
        """
        target = self._resolve_target(code)
        # End the read transaction before the write so SQLite (WAL) does not fail the
        # read-to-write lock upgrade with SQLITE_BUSY_SNAPSHOT. This is a transaction
        # boundary, not an additional statement.
        self.session.rollback()
        try:
            self.click_repo.add(
                ClickEvent(
                    link_id=target.id,
                    referrer=referrer[:MAX_REFERRER_LEN] if referrer else None,
                    user_agent=user_agent[:MAX_USER_AGENT_LEN] if user_agent else None,
                    ip_hash=hash_ip(client_ip, self.ip_hash_salt),
                )
            )
            self.session.commit()
        except Exception as exc:
            # A secondary rollback failure is suppressed: the original failure is logged.
            with contextlib.suppress(Exception):
                self.session.rollback()
            logger.warning(
                "failed to record click for link_id=%s (%s)", target.id, type(exc).__name__
            )
        return target.url
