"""Password hashing helpers. bcrypt hashes start with $2; legacy rows stay plaintext until login."""
from __future__ import annotations

import bcrypt

_BCRYPT_PREFIXES = (b"$2a$", b"$2b$", b"$2y$")


def hash_password(password: str) -> str:
    raw = (password or "").encode("utf-8")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def is_hashed(stored: str | None) -> bool:
    if not stored:
        return False
    value = stored.encode("utf-8") if isinstance(stored, str) else stored
    return value[:4] in _BCRYPT_PREFIXES


def verify_password(password: str, stored: str | None) -> bool:
    if stored is None or password is None:
        return False
    if is_hashed(stored):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
        except (ValueError, TypeError):
            return False
    return stored == password


def needs_rehash(stored: str | None) -> bool:
    return bool(stored) and not is_hashed(stored)
