"""Password-reset token hashing. Store only the digest; email the raw token."""
from __future__ import annotations

import hashlib


def hash_reset_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()
