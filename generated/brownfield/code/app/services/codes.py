"""Short-code generation and validation helpers.

Codes are drawn from the base62 alphabet with a CSPRNG (``secrets``); the ``random``
module is deliberately never used so codes stay unguessable.

``RESERVED_CODES`` is the single source of truth for path segments that neither
generated codes nor client-chosen aliases may take. Generated codes are validated
with the base62-only ``is_valid_code_format``; aliases use ``is_valid_alias`` and
lookups on code-addressed endpoints use the wider ``is_valid_lookup_code``.
"""

import re
import secrets
import string

ALPHABET = string.ascii_letters + string.digits

RESERVED_CODES = frozenset(
    {
        "healthz",
        "readyz",
        "api",
        "docs",
        "redoc",
        "openapi.json",
        "static",
        "favicon.ico",
        "metrics",
    }
)

CODE_PATTERN = re.compile(r"^[A-Za-z0-9]{1,32}$")

ALIAS_MIN_LENGTH = 3
ALIAS_MAX_LENGTH = 32
ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
LOOKUP_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def generate_code(length: int) -> str:
    """Return a random base62 code of ``length`` characters."""
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def is_reserved(code: str) -> bool:
    """Return True if ``code`` collides (case-insensitively) with a reserved path segment."""
    return code.casefold() in RESERVED_CODES


def is_valid_code_format(code: str) -> bool:
    """Return True if ``code`` could be a stored short code (1-32 base62 characters)."""
    return bool(CODE_PATTERN.fullmatch(code))


def is_valid_alias(alias: str) -> bool:
    """Return True if ``alias`` is 3-32 ASCII characters of A-Z, a-z, 0-9, ``_`` or ``-``.

    Uses ``fullmatch`` so a trailing newline never matches. Reserved words are not
    checked here; see ``is_reserved``.
    """
    return bool(ALIAS_PATTERN.fullmatch(alias))


def is_valid_lookup_code(code: str) -> bool:
    """Return True if ``code`` may address a stored link (generated code or alias).

    Accepts 1-32 ASCII characters of A-Z, a-z, 0-9, ``_`` or ``-``. Anything else can
    never be stored, so callers can answer 404 without querying the database.
    """
    return bool(LOOKUP_CODE_PATTERN.fullmatch(code))
