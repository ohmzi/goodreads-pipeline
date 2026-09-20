"""Credential encryption.

Credentials are entered through the web UI and stored encrypted, so the
database can be copied around for debugging without leaking passwords. The
key lives in .env (mode 600) — losing it means re-entering everything, which
is a deliberate trade against baking secrets into a config file.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


class CredentialCipher:
    def __init__(self, secret: str):
        if not secret:
            # Refuse loudly. A silent fallback would write credentials that
            # look stored but cannot be read back after a restart.
            raise RuntimeError(
                "GOODREADS_SECRET_KEY is not set. Generate one with:\n"
                "  python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, blob: bytes) -> str:
        try:
            return self._fernet.decrypt(blob).decode()
        except InvalidToken as exc:
            raise RuntimeError(
                "Stored credential could not be decrypted — GOODREADS_SECRET_KEY "
                "has changed since it was saved. Re-enter it in Settings."
            ) from exc


_cipher: CredentialCipher | None = None


def cipher() -> CredentialCipher:
    global _cipher
    if _cipher is None:
        _cipher = CredentialCipher(settings.secret_key)
    return _cipher
