"""Link lifecycle business rules: creation, lookup, deactivation and redirects.

The service owns transaction boundaries: repositories only flush, and this module
alone calls ``commit()``/``rollback()``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    BadRequestError,
    ConflictError,
    GoneError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.models import ClickEvent, IdempotencyKey, Link, utcnow
from app.repositories import ClickRepository, IdempotencyRepository, LinkRepository
from app.services import codes

logger = logging.getLogger("app.services.links")

MAX_REFERRER_LEN = 2048
MAX_USER_AGENT_LEN = 512
MAX_IDEMPOTENCY_KEY_LEN = 255


@dataclass
class CreateResult:
    """Outcome of ``create_link``; ``replayed`` is True for an idempotent replay."""

    link: Link
    replayed: bool


@dataclass
class LinkDetail:
    """A link (active or not) together with its all-time click count."""

    link: Link
    total_clicks: int


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
    ) -> None:
        self.session = session
        self.code_length = code_length
        self.max_attempts = max_attempts
        self.ip_hash_salt = ip_hash_salt
        self.link_repo = LinkRepository(session)
        self.click_repo = ClickRepository(session)
        self.idem_repo = IdempotencyRepository(session)

    def create_link(self, url: str, idempotency_key: str | None = None) -> CreateResult:
        """Create a new active link, honouring an optional Idempotency-Key.

        Raises:
            BadRequestError: the key is blank or longer than 255 characters.
            ConflictError: the key was already used with a different URL.
            ServiceUnavailableError: no unique code could be allocated.
        """
        key = idempotency_key
        if key is not None and (key.strip() == "" or len(key) > MAX_IDEMPOTENCY_KEY_LEN):
            raise BadRequestError("Invalid Idempotency-Key header")

        request_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()

        if key is not None:
            existing = self.idem_repo.get_by_key(key)
            if existing is not None:
                return self._replay_or_conflict(existing, request_hash)

        for _attempt in range(self.max_attempts):
            code = codes.generate_code(self.code_length)
            if codes.is_reserved(code):
                continue

            link = Link(code=code, url=url, is_active=True)
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

    def _replay_or_conflict(self, record: IdempotencyKey, request_hash: str) -> CreateResult:
        """Replay the link stored for ``record`` or raise if the payload differs."""
        if record.request_hash != request_hash:
            raise ConflictError("Idempotency-Key already used with a different payload")
        link = self.session.get(Link, record.link_id)
        if link is None:
            raise ConflictError("Idempotency-Key conflict, please retry")
        return CreateResult(link=link, replayed=True)

    def _get_or_404(self, code: str) -> Link:
        """Return the link for ``code`` or raise ``NotFoundError`` (malformed codes never query)."""
        if not codes.is_valid_code_format(code):
            raise NotFoundError()
        link = self.link_repo.get_by_code(code)
        if link is None:
            raise NotFoundError()
        return link

    def get_link_detail(self, code: str) -> LinkDetail:
        """Return a link (active or deactivated) and its all-time click count."""
        link = self._get_or_404(code)
        return LinkDetail(link=link, total_clicks=self.click_repo.count_for_link(link.id))

    def deactivate(self, code: str) -> None:
        """Soft-delete a link. Idempotent: an inactive link keeps its deactivated_at."""
        link = self._get_or_404(code)
        if link.is_active:
            link.is_active = False
            link.deactivated_at = utcnow()
            self.session.commit()

    def resolve_for_redirect(self, code: str) -> str:
        """Return the target URL of an active link without recording a click (HEAD)."""
        link = self._get_or_404(code)
        if not link.is_active:
            raise GoneError()
        return link.url

    def resolve_and_record_click(
        self,
        code: str,
        referrer: str | None,
        user_agent: str | None,
        client_ip: str | None,
    ) -> str:
        """Return the target URL of an active link and record a click (best-effort).

        A failure while recording the click is rolled back and logged (never with the
        client IP); the URL is still returned so the redirect succeeds.
        """
        link = self._get_or_404(code)
        if not link.is_active:
            raise GoneError()
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
