"""Short-code generation and validation helpers.

Codes are drawn from the base62 alphabet with a CSPRNG (``secrets``); the ``random``
module is deliberately never used so codes stay unguessable.
"""

import re
import secrets
import string

ALPHABET = string.ascii_letters + string.digits

RESERVED_CODES = frozenset(
    {"healthz", "readyz", "api", "docs", "redoc", "openapi.json", "static", "favicon.ico"}
)

CODE_PATTERN = re.compile(r"^[A-Za-z0-9]{1,32}$")


def generate_code(length: int) -> str:
    """Return a random base62 code of ``length`` characters."""
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def is_reserved(code: str) -> bool:
    """Return True if ``code`` collides (case-insensitively) with a reserved path segment."""
    return code.casefold() in RESERVED_CODES


def is_valid_code_format(code: str) -> bool:
    """Return True if ``code`` could be a stored short code (1-32 base62 characters)."""
    return bool(CODE_PATTERN.fullmatch(code))
