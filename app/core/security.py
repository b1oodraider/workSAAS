"""Password hashing with stdlib scrypt (no extra dependencies)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading

_N, _R, _P = 2**14, 8, 1
# Each scrypt call takes ~16 MB (128 * r * N bytes): bound how many run at once, so a burst of
# parallel login attempts cannot exhaust the server's memory (the rest wait their turn).
_SCRYPT_SLOTS = threading.BoundedSemaphore(4)


def _scrypt(password: str, salt: bytes, dklen: int) -> bytes:
    with _SCRYPT_SLOTS:
        return hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=dklen)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = _scrypt(password, salt, 32)
    return "scrypt${}${}".format(
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    salt = base64.b64decode(salt_b64)
    expected = base64.b64decode(digest_b64)
    digest = _scrypt(password, salt, len(expected))
    return hmac.compare_digest(digest, expected)


def session_stamp(password_hash: str) -> str:
    """Stored in the session cookie: changing the password logs out every other session."""
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]
