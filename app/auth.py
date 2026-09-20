"""Authentication.

This app is the most sensitive service on this host: it holds the key that
decrypts every stored credential, and it can drive a browser that is logged
into a real personal account. So the rules here are deliberately conservative.

  * Passwords are hashed with scrypt (memory-hard, in the stdlib) and compared
    in constant time. No plaintext or reversibly-encrypted password is stored,
    because unlike a service API key there is never a reason to read one back.
  * Sessions are stateless HMAC-signed tokens, so there is no session table to
    leak and nothing to garbage-collect. The signature is verified in constant
    time before the payload is parsed.
  * The cookie is HttpOnly and SameSite=Lax. Lax is what stops another site
    POSTing to /api/... with the user's cookie attached — it withholds cookies
    on cross-site POST, which is the CSRF case that matters here.
  * Failed logins are throttled per username and per client address, with the
    comparison done in constant time so a wrong username and a wrong password
    take the same work.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time

from .config import settings
from .db import db

COOKIE_NAME = "goodreads_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600

# scrypt cost. n=2**14 with r=8 needs ~16 MB and a few ms — enough to make
# offline guessing expensive without stalling a login on this hardware.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


# --------------------------------------------------------------------------
# passwords
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """`scrypt$n$r$p$salt$hash`, self-describing so the cost can be raised later."""
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(derived)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_b64d(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(_b64d(hash_b64)),
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(derived, _b64d(hash_b64))


# --------------------------------------------------------------------------
# session tokens
# --------------------------------------------------------------------------
def _signing_key() -> bytes:
    if not settings.secret_key:
        raise RuntimeError("GOODREADS_SECRET_KEY is not set; cannot sign sessions.")
    return hashlib.sha256(f"goodreads-session:{settings.secret_key}".encode()).digest()


def issue_session(username: str, epoch: str, ttl: int = SESSION_TTL_SECONDS) -> str:
    payload = {
        "u": username,
        # Binds the token to the password it was issued against, so changing
        # the password evicts every existing session.
        "e": epoch,
        "exp": int(time.time()) + ttl,
        # A per-token nonce so two logins never produce the same cookie.
        "n": secrets.token_urlsafe(12),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(_signing_key(), raw, hashlib.sha256).digest()
    return f"{_b64e(raw)}.{_b64e(signature)}"


def read_session(token: str | None) -> dict | None:
    """Return the payload if the token is authentic and unexpired, else None."""
    if not token or "." not in token:
        return None
    raw_b64, sig_b64 = token.split(".", 1)
    try:
        raw = _b64d(raw_b64)
        signature = _b64d(sig_b64)
    except (ValueError, TypeError):
        return None

    expected = hmac.new(_signing_key(), raw, hashlib.sha256).digest()
    # Constant-time, and before any parsing of attacker-controlled bytes.
    if not hmac.compare_digest(signature, expected):
        return None

    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or "u" not in payload:
        return None
    try:
        if int(payload.get("exp", 0)) < time.time():
            return None
    except (TypeError, ValueError):
        return None
    return payload


def session_username(token: str | None) -> str | None:
    """The signed-in username, or None.

    Verifies the password epoch too, so a token minted before a password change
    stops working the moment that password changes.
    """
    payload = read_session(token)
    if not payload:
        return None
    username = str(payload["u"])
    if str(payload.get("e", "")) != db().session_epoch_for(username):
        return None
    return username


def new_epoch() -> str:
    return secrets.token_urlsafe(16)


# --------------------------------------------------------------------------
# login throttling
# --------------------------------------------------------------------------
class LoginThrottle:
    """Progressive delay on failed sign-ins.

    Deliberately NOT a hard lockout. A lockout that refuses *even the correct
    password* is a denial of service an unauthenticated attacker can trigger on
    demand against a known username — and on a single-user service that means
    locking the owner out of their own UI for as long as the attacker keeps
    knocking. Instead each wrong guess costs progressively more time, which
    bounds guessing rate without ever refusing a right answer.

    In-memory on purpose: single-process service, and a restart clearing the
    counters is preferable to persisting an attacker's traffic to disk.
    """

    def __init__(
        self,
        window: int = 900,
        free_attempts: int = 3,
        base_delay: float = 0.5,
        max_delay: float = 8.0,
        max_keys: int = 4096,
    ):
        self.window = window
        self.free_attempts = free_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.max_keys = max_keys
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    def _prune(self, key: str, now: float) -> list[float]:
        attempts = [t for t in self._failures.get(key, []) if now - t < self.window]
        if attempts:
            self._failures[key] = attempts
        else:
            self._failures.pop(key, None)
        return attempts

    def record_failure(self, key: str) -> None:
        now = time.time()
        with self._lock:
            attempts = self._prune(key, now)
            attempts.append(now)
            self._failures[key] = attempts
            # Bound memory: the key includes a client-supplied address, so
            # without this an attacker could grow the dict without limit.
            if len(self._failures) > self.max_keys:
                for stale in sorted(self._failures, key=lambda k: self._failures[k][-1])[
                    : len(self._failures) - self.max_keys
                ]:
                    self._failures.pop(stale, None)

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def delay_for(self, key: str) -> float:
        """How long this key must wait before its next attempt is answered."""
        now = time.time()
        with self._lock:
            attempts = self._prune(key, now)
        extra = len(attempts) - self.free_attempts
        if extra <= 0:
            return 0.0
        return min(self.base_delay * (2 ** (extra - 1)), self.max_delay)

    def recent_failures(self, key: str) -> int:
        now = time.time()
        with self._lock:
            return len(self._prune(key, now))


throttle = LoginThrottle()


# --------------------------------------------------------------------------
# bootstrap
# --------------------------------------------------------------------------
def generate_password(length: int = 20) -> str:
    """A readable-but-strong password: no shell-hostile characters."""
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))
