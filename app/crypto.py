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


#: Shortest secret accepted. Not an argument about the KDF — there is no KDF
#: here beyond one SHA-256 pass, so the secret is exactly as strong as the
#: string in `.env` and has to carry the entropy itself. 32 generated
#: URL-safe characters is ~192 bits; the documented value is 48 bytes
#: (`token_urlsafe(48)`), and this is the floor below which a deploy is
#: stopped rather than left quietly weak. `app/cli.py` checks the same
#: constant itself, so its refusal stays the documented exit 2.
MIN_SECRET_LENGTH = 32


class CredentialCipher:
    def __init__(self, secret: str):
        if not secret:
            # Refuse loudly. A silent fallback would write credentials that
            # look stored but cannot be read back after a restart.
            raise RuntimeError(
                "GOODREADS_SECRET_KEY is not set. Generate one with:\n"
                "  python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        if len(secret) < MIN_SECRET_LENGTH:
            # A message of its own, deliberately: the empty case above is a
            # contract an operator is told to look for ("is not set" — see
            # docs/OPERATIONS.md), and a short key is a different problem with
            # a different answer. Collapsing the two would send someone to
            # check a variable that is set perfectly well.
            raise RuntimeError(
                f"GOODREADS_SECRET_KEY is set but shorter than "
                f"{MIN_SECRET_LENGTH} characters. Nothing stretches it before "
                f"use, so it is exactly as strong as this string. Generate one "
                f"with:\n"
                f"  python -c \"import secrets; print(secrets.token_urlsafe(48))\""
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
