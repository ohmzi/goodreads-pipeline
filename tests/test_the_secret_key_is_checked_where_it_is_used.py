"""The secret key's own strength, and where that is allowed to be enforced.

Nothing stretches `GOODREADS_SECRET_KEY`: the Fernet key is one SHA-256 pass
over the string in `.env`, and the session signing key is another. The secret
is exactly as strong as what is in the file, so a short one is worth refusing —
but only where a credential is used. Raising at boot would turn a working
deployment into a crash loop under `restart: unless-stopped`.
"""

from __future__ import annotations

import argparse
import inspect

import pytest

from app.crypto import MIN_SECRET_LENGTH, CredentialCipher


def test_an_empty_key_keeps_its_documented_message():
    """docs/OPERATIONS.md tells an operator to look for "is not set"."""
    with pytest.raises(RuntimeError) as caught:
        CredentialCipher("")

    assert "is not set" in str(caught.value)


def test_a_short_key_gets_a_message_of_its_own():
    """A different problem with a different answer — sending someone to check a
    variable that is set perfectly well wastes their afternoon."""
    with pytest.raises(RuntimeError) as caught:
        CredentialCipher("x" * (MIN_SECRET_LENGTH - 1))

    message = str(caught.value)
    assert "shorter than" in message
    assert "is not set" not in message


def test_the_floor_itself_is_accepted():
    assert CredentialCipher("x" * MIN_SECRET_LENGTH).decrypt(
        CredentialCipher("x" * MIN_SECRET_LENGTH).encrypt("ok")
    ) == "ok"


def test_the_key_this_suite_runs_on_clears_the_floor():
    """Otherwise every test here would be exercising a shape that cannot
    start, and the failures would look like anything but a short key."""
    from app.config import settings

    assert len(settings.secret_key) >= MIN_SECRET_LENGTH


def test_the_cli_refuses_a_short_key_as_an_exit_code(monkeypatch, capsys):
    """A documented refusal — not a traceback out of a constructor, which is
    what calling the cipher's validator from here would have produced."""
    from app import cli

    monkeypatch.setattr(cli.settings, "secret_key", "too-short")
    args = argparse.Namespace(username="operator", generate=True, length=20)

    assert cli.cmd_set_password(args) == 2
    assert "shorter than" in capsys.readouterr().err


def test_the_startup_hook_does_not_validate_the_key():
    """The check belongs where a credential is used.

    A deployment whose key predates this floor must keep serving; failing at
    start-up would crash-loop it instead, under `restart: unless-stopped`, with
    nobody watching and no way in.
    """
    from app import main

    assert "cipher" not in inspect.getsource(main._startup)
