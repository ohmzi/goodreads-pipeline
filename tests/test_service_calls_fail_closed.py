"""What a service client does with an answer it should not accept.

Three answers are refused: a redirect, a body too large to be one of ours, and
— for a client whose request carries a credential — any body at all coming back
in a diagnostic string. Each refusal has to stay loud rather than be filed as
an outage, because `pipeline._forgive_transient` forgives anything the breaker
counts as transient for 24 hours, and a misconfiguration forgiven for a day is
169 silently parked books.
"""

from __future__ import annotations

import gzip
import json

import httpx
import pytest

from app.clients.base import ClientError


def _client(monkeypatch, cls, handler, base="http://service"):
    """A real client of `cls`, with only the socket replaced.

    Deliberately not `cls(base)` followed by swapping `_client`: the point is
    to exercise the configuration `__init__` actually applies — `follow_redirects`
    among it — so only the transport is substituted.
    """
    real = httpx.Client

    def build(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", build)
    return cls(base)


def _store_kavita_key() -> None:
    """Kavita puts its key on every call, so a client without one raises
    `require_cred` and never reaches the transport at all."""
    from app.crypto import cipher
    from app.db import db

    db().set_credential("kavita_api_key", cipher().encrypt("a-test-api-key"))


def test_clients_are_not_configured_to_follow_redirects():
    from app.clients.base import ServiceClient

    assert ServiceClient("http://service")._client.follow_redirects is False


def test_a_redirect_is_refused_and_stays_loud(monkeypatch):
    """Not followed, and not filed as an outage.

    An `httpx.HTTPError` would carry no status, so `kind` would derive
    "network" — transient — and the breaker would both count it and forgive it,
    holding a service whose URL is simply wrong and parking its books.
    """
    from app import breaker
    from app.clients.kavita import KavitaClient

    _store_kavita_key()

    def handler(request):
        return httpx.Response(302, headers={"location": "http://elsewhere.invalid/api"})

    client = _client(monkeypatch, KavitaClient, handler)
    with pytest.raises(ClientError) as caught:
        client.libraries()

    assert caught.value.status == 302
    assert caught.value.kind == "auth"
    assert caught.value.kind not in breaker.TRANSIENT_KINDS
    assert "elsewhere.invalid" in str(caught.value)


def test_a_redirect_with_nothing_to_redirect_to_still_fails(monkeypatch):
    """A 3xx with no `Location` is not a success, and must not read as one."""
    from app.clients.shelfmark import ShelfmarkClient

    def handler(request):
        return httpx.Response(303)

    client = _client(monkeypatch, ShelfmarkClient, handler)
    with pytest.raises(ClientError) as caught:
        client.request("GET", "/api/status", "task status")

    assert caught.value.status == 303
    assert "without giving a Location" in str(caught.value)


def test_a_gzipped_body_comes_back_readable(monkeypatch):
    """The positive control the response cap needs.

    A plaintext body never exercises the rebuffering at all: `iter_bytes()`
    hands back *decoded* bytes, and a response rebuilt with `Content-Encoding`
    still on it runs the decoder over them a second time and raises
    `DecodingError` — which is an `httpx.HTTPError`, so it would be reported as
    the service being unreachable and then forgiven for a day. Only a
    compressed upstream catches that, which is why the test uses one.
    """
    from app.clients.kavita import KavitaClient

    payload = json.dumps({"series": [{"id": 1, "name": "Books"}]}).encode()

    def handler(request):
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, content=gzip.compress(payload)
        )

    client = _client(monkeypatch, KavitaClient, handler)
    resp = client.request("GET", "/api/Search/search", "search")

    assert resp.json() == {"series": [{"id": 1, "name": "Books"}]}
    assert resp.status_code == 200


def test_an_oversized_body_is_refused_not_truncated(monkeypatch):
    """Half a JSON document is a parse error somewhere else, which reads as
    the service being broken rather than as the answer having been too big."""
    from app.clients import base as base_mod
    from app.clients.shelfmark import ShelfmarkClient

    monkeypatch.setattr(base_mod, "MAX_RESPONSE_BYTES", 1024)

    def handler(request):
        return httpx.Response(200, content=b"x" * 8192)

    client = _client(monkeypatch, ShelfmarkClient, handler)
    with pytest.raises(ClientError) as caught:
        client.request("GET", "/api/releases", "release search")

    assert caught.value.status == 200
    assert "more than" in str(caught.value)
    assert caught.value.kind == "server"


def test_a_body_at_the_cap_is_still_read(monkeypatch):
    """An off-by-one that refused the boundary would be a real outage."""
    from app.clients import base as base_mod
    from app.clients.shelfmark import ShelfmarkClient

    monkeypatch.setattr(base_mod, "MAX_RESPONSE_BYTES", 1024)

    def handler(request):
        return httpx.Response(200, content=b"x" * 1024)

    client = _client(monkeypatch, ShelfmarkClient, handler)
    assert len(client.request("GET", "/x", "read").content) == 1024


def test_a_credential_bearing_client_does_not_quote_the_body(monkeypatch):
    """The credential stays out of `stage_runs.detail`.

    The `credentials` table is encrypted at rest; a diagnostic string that
    echoes a response is not, and a service that mirrors its request — or an
    endpoint that is not the service at all — would put the password there in
    clear.
    """
    from app.clients.kavita import KavitaClient

    _store_kavita_key()

    def handler(request):
        return httpx.Response(401, text="apiKey=super-secret-value rejected")

    client = _client(monkeypatch, KavitaClient, handler)
    with pytest.raises(ClientError) as caught:
        client.libraries()

    assert "super-secret-value" not in str(caught.value)
    assert caught.value.status == 401
    # Said rather than omitted, so an operator knows the response said more.
    assert "withheld" in str(caught.value)


def test_a_client_with_no_credential_still_quotes_its_body(monkeypatch):
    """Shelfmark takes no credential, and its bodies are the whole diagnosis.

    Every live `stage_runs.detail` that quotes a response body comes from this
    one client — so withholding here would cost the explanation and buy
    nothing.
    """
    from app.clients.shelfmark import ShelfmarkClient

    def handler(request):
        return httpx.Response(
            503, text='{"error":"Unable to reach download source"}'
        )

    client = _client(monkeypatch, ShelfmarkClient, handler)
    with pytest.raises(ClientError) as caught:
        client.request("GET", "/api/status", "task status")

    assert "Unable to reach download source" in str(caught.value)


def _store_booklore_credentials() -> None:
    from app.crypto import cipher
    from app.db import db

    for key, value in (("booklore_username", "operator"),
                       ("booklore_password", "a-password")):
        db().set_credential(key, cipher().encrypt(value))


def _booklore(monkeypatch, handler):
    """A BookLore client with an empty token cache.

    The cache is module-level and keyed by base URL, so leaving it populated
    would make one test's token the next test's starting state.
    """
    from app.clients import booklore

    booklore._TOKEN_CACHE.clear()
    monkeypatch.setattr(booklore.time, "sleep", lambda _seconds: None)
    _store_booklore_credentials()
    return _client(monkeypatch, booklore.BookLoreClient, handler)


def test_a_conflict_is_still_recognised_with_the_body_withheld(monkeypatch):
    """The regression the body-withholding nearly caused.

    BookLore answers a second login with 400 while a previous refresh token is
    live, and the retry is the only reason a login-per-call client works at all
    — its own module docstring says so. That branch used to read the message,
    which no longer contains the body, so it now reads `exc.body` directly.
    """
    from app.clients.booklore import BookLoreClient

    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(400, json={"message": "A data conflict occurred."})
        return httpx.Response(200, json={"accessToken": "a-token"})

    client = _booklore(monkeypatch, handler)

    assert client.login() == "a-token"
    assert len(seen) == 2, "the conflict was not retried"


def test_a_wrong_password_is_not_retried_as_a_conflict(monkeypatch):
    """BookLore answers 400 to a bad password too — verified against the live
    service — so the status alone cannot stand in for the body. Branching on it
    would sleep and retry for every typo."""
    from app.clients.booklore import BookLoreClient

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(400, json={"message": "Invalid credentials"})

    client = _booklore(monkeypatch, handler)

    with pytest.raises(ClientError):
        client.login()
    assert len(seen) == 1, "a wrong password was retried"


def test_the_body_is_readable_but_never_in_the_message(monkeypatch):
    """Both halves at once, because either alone would pass while the other
    was broken: the caller can branch on it, and nothing that gets logged,
    persisted or shown can contain it."""
    from app.clients.kavita import KavitaClient

    _store_kavita_key()

    def handler(request):
        return httpx.Response(500, text="apiKey=super-secret-value blew up")

    client = _client(monkeypatch, KavitaClient, handler)
    with pytest.raises(ClientError) as caught:
        client.libraries()

    assert "super-secret-value" in caught.value.body, "the caller cannot branch on it"
    assert "super-secret-value" not in str(caught.value), "it reached the message"


def test_every_authenticating_service_withholds_its_bodies():
    """Driven off the registry, so a service added later cannot quietly be the
    exception: the only client that may quote a body is one that sends nothing.
    """
    from app import health

    for name, _label, factory in health.SERVICES:
        if name == "shelfmark":
            assert factory.quotes_error_bodies is True, name
        else:
            assert factory.quotes_error_bodies is False, name
