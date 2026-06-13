"""Authentication primitives.

Two distinct kinds of secret live here:

* **User passwords** — low-entropy, human-chosen. Hashed with bcrypt. To sidestep
  bcrypt's 72-byte input cap (and to make migrating the old SHA-256 PIN hashes a
  one-shot operation), we bcrypt the *SHA-256 hex* of the password rather than the
  password itself. A legacy hash is exactly ``sha256_hex(pin)``, so bcrypt-ing a
  legacy hash in place yields the same value ``hash_password(pin)`` would — no
  plaintext required to migrate.
* **Opaque tokens** — session tokens and static API keys. These are high-entropy
  random strings, so a fast SHA-256 of the token is a perfectly safe stored form;
  bcrypt would only add latency to every request.
"""

from __future__ import annotations

import hashlib
import re
import secrets

import bcrypt

_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# --- user passwords -------------------------------------------------------

def hash_password(plain: str) -> str:
    """Return the bcrypt-over-SHA256 hash of a user password."""
    digest = _sha256_hex(plain).encode("ascii")
    return bcrypt.hashpw(digest, bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, stored: str | None) -> bool:
    """Constant-time check of a password against a stored bcrypt-over-SHA256 hash."""
    if not stored:
        return False
    try:
        digest = _sha256_hex(plain).encode("ascii")
        return bcrypt.checkpw(digest, stored.encode("ascii"))
    except (ValueError, TypeError):
        return False


def wrap_legacy_hash(legacy_sha256_hex: str) -> str:
    """Migrate a legacy ``sha256(pin)`` hash to the new format without the plaintext.

    Because ``legacy_sha256_hex == sha256_hex(pin)``, bcrypt-ing it directly is
    identical to ``hash_password(pin)`` and will verify against the user's PIN.
    """
    return bcrypt.hashpw(legacy_sha256_hex.encode("ascii"), bcrypt.gensalt()).decode("ascii")


def is_bcrypt_hash(value: str | None) -> bool:
    """True if ``value`` is already a bcrypt hash (i.e. has been migrated)."""
    return bool(value) and value.startswith(_BCRYPT_PREFIXES)


# --- opaque tokens (sessions, static API keys) ----------------------------

def generate_token() -> str:
    """A new opaque token to hand to a client exactly once (~256 bits)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Stored form of a session token or static API key."""
    return _sha256_hex(token)


def token_prefix(token: str, length: int = 8) -> str:
    """Short, non-secret identifier for displaying which key is which."""
    return token[:length]


# --- usernames ------------------------------------------------------------

_USERNAME_ALLOWED = re.compile(r"[^a-z0-9._-]+")


def slugify_username(display_name: str) -> str:
    """Derive a safe login handle from a display name (used for migration backfill)."""
    base = _USERNAME_ALLOWED.sub("", display_name.strip().lower().replace(" ", ""))
    return base or "user"
