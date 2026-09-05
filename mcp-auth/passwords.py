"""Password verification, byte-compatible with the configuration service.

The format is deliberately identical to ``config-ui/control_plane.py``'s
``password_hash``/``verify_password`` so that P4's migration of the administrator
credential into the ``control`` schema is a copy rather than a re-hash. Do not
change the algorithm, the round count or the encoding here without changing it
there in the same commit.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

#: Must match config-ui/control_plane.py:PBKDF2_ROUNDS.
PBKDF2_ROUNDS = 310_000
ALGORITHM = "pbkdf2-sha256"


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return (
        f"{ALGORITHM}${PBKDF2_ROUNDS}"
        f"${base64.urlsafe_b64encode(salt).decode()}"
        f"${base64.urlsafe_b64encode(digest).decode()}"
    )


def verify_password(password: str, encoded: str) -> bool:
    if not isinstance(password, str) or not isinstance(encoded, str):
        return False
    try:
        algorithm, rounds, salt, expected = encoded.split("$", 3)
        if algorithm != ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            base64.urlsafe_b64decode(salt),
            int(rounds),
        )
        return hmac.compare_digest(base64.urlsafe_b64encode(digest).decode(), expected)
    except (TypeError, ValueError):
        return False
