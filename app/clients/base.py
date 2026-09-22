"""Shared plumbing for the service clients.

Every client raises `ClientError` carrying the service name and the real HTTP
status, because the whole point of the Settings page is that a bad credential
reads as "401 from Kavita" rather than a stage that silently does nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from ..crypto import cipher
from ..db import db

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

#: Largest response body read from a service, in bytes. Nothing here expects a
#: large payload — a library listing is about a megabyte — so a service
#: answering with more than this has either malfunctioned or is not the service
#: we believe we are talking to. The body is refused whole rather than
#: truncated: half a JSON document is a parse error somewhere else, which reads
#: as the service being broken rather than as the answer having been too big.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

#: Headers that describe a *transfer* rather than a body. A rebuilt response is
#: handed bytes `iter_bytes()` has already decoded and de-framed, so leaving any
#: of these on it is a claim about the bytes that is no longer true:
#: `httpx.Response.__init__` would run the named decoder over plain bytes and
#: raise `DecodingError`, which is an `httpx.HTTPError` — so it would surface as
#: a *network* failure, and `pipeline._forgive_transient` would then forgive
#: every failing book for 24 hours. It is not a hypothetical: a gzipped upstream
#: reproduced exactly that while this was written. httpx recomputes
#: `Content-Length` from the content it is given.
_TRANSFER_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})


def _retry_after(resp: httpx.Response) -> float | None:
    """How long the service asked us to wait, in seconds, or None.

    `Retry-After` is either a number of seconds or an HTTP-date. Both forms are
    accepted because the spec allows either and the cost of getting it wrong is
    a cooldown that ignores the one piece of timing evidence a rate limiter
    ever gives us.
    """
    raw = (resp.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class ClientError(RuntimeError):
    def __init__(self, service: str, message: str, status: int | None = None,
                 kind: str | None = None, retry_after: float | None = None,
                 body: str = ""):
        self.service = service
        self.status = status
        #: The start of the response body, offered **only so a caller can branch
        #: on it**. It is not part of `str(self)`, and it must never be
        #: interpolated into anything persisted, shown, or handed to `log()` —
        #: for a request that carried a credential this is exactly the text
        #: `quotes_error_bodies` exists to keep out of `stage_runs.detail`.
        #:
        #: `booklore.login` is the only reader, and it has to read it: BookLore
        #: answers **400 both** when a previous refresh token is still live and
        #: when the password is simply wrong (Grimmory, the fork, answers 401 to
        #: the second), so the status cannot tell them apart and the text is the
        #: only signal. Holding it in memory adds no exposure — the credential
        #: it might echo is one this process just sent.
        self.body = body
        #: Set only where the status code cannot express the cause — see
        #: `require_cred`. `None` means "derive it from the status".
        self._kind = kind
        #: Seconds the service asked us to wait, if it said. Carried here
        #: rather than reconstructed later because the header is gone by the
        #: time the failure reaches the scheduler.
        self.retry_after = retry_after
        super().__init__(f"{service}: {message}")

    @property
    def kind(self) -> str:
        """Why this failed, at a level the UI can act on.

        The distinction that matters operationally is *auth* versus everything
        else: an auth failure needs a human to open Settings, whereas a network
        or 5xx failure just needs another attempt. Without this the two look
        identical in a stage's detail string and the operator cannot tell which
        failures are worth their attention.
        """
        if self._kind is not None:
            return self._kind
        if self.status in (401, 403):
            return "auth"
        if self.status == 404:
            return "notfound"
        if self.status in (408, 425, 429):
            # Refusing work *right now*, not broken: the same "try again"
            # answer as a 5xx, but named separately so a message can say "too
            # many requests" rather than "the service returned an error".
            # Anna's Archive answers 429 to concurrent searches, which is
            # exactly the load the pipeline's own worker pool creates.
            #
            # Before this arm, a 429 fell through to "" — unclassified — so it
            # was neither forgiven by `_forgive_transient` (which gates on the
            # kind) nor grouped with its service, and every affected book was
            # parked on its first attempt.
            return "busy"
        if self.status is not None and self.status >= 500:
            return "server"
        if self.status is None:
            # No HTTP status at all means the request never landed.
            return "network"
        return ""


def cred(key: str, default: str = "") -> str:
    """Fetch and decrypt a stored credential, or fall back to `default`."""
    blob = db().get_credential(key)
    if blob is None:
        return default
    return cipher().decrypt(blob)


def require_cred(key: str, label: str) -> str:
    value = cred(key)
    if not value:
        # `kind="auth"` is explicit because the status-based derivation cannot
        # see this case at all: there is no HTTP status, so it fell through to
        # "network", and `pipeline._forgive_transient` then treated a
        # misconfiguration as an outage — forgiving a missing API key for 24
        # hours and never telling anyone to go and set it.
        raise ClientError(
            "settings",
            f"{label} is not set. Add it on the Settings page.",
            kind="auth",
        )
    return value


class ServiceClient:
    """Base for the thin per-service wrappers."""

    name = "service"
    base_url = ""

    #: Whether a failure may quote the service's own response body back.
    #:
    #: False on every client that authenticates, because such a request carries
    #: a credential — a password in a login body, an API key or bearer token in
    #: a header — and a response that echoes its request would write that
    #: credential in clear into `stage_runs.detail`. That is the one invariant
    #: the `credentials` table's encryption exists to keep, and a diagnostic
    #: string is not worth breaking it for. Removing the submitted value from
    #: the text is not an alternative: the echo can be encoded, truncated or
    #: reworded, and the check would have to enumerate what to look for.
    #:
    #: Shelfmark keeps it: that instance takes no credential at all, and its
    #: bodies are where a "down for 14h of 24h" explanation comes from.
    quotes_error_bodies = True

    def __init__(self, base_url: str | None = None):
        if base_url:
            self.base_url = base_url.rstrip("/")
        # `follow_redirects=False`, refused in `_check` rather than chased. A
        # service is addressed directly by URL, so a redirect is a
        # misconfiguration at best and a way to move a credential-carrying
        # request to a host nobody configured at worst.
        self._client = httpx.Client(
            base_url=self.base_url, timeout=DEFAULT_TIMEOUT, follow_redirects=False
        )

    def __enter__(self) -> "ServiceClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- request helpers -------------------------------------------------
    def _check(self, resp: httpx.Response, what: str) -> httpx.Response:
        if 300 <= resp.status_code < 400:
            # Refused, never followed. Every service here is addressed directly,
            # so a redirect means something is configured differently than this
            # client believes — and following it could deliver credentials to a
            # host nobody chose.
            #
            # `kind="auth"` is explicit and load-bearing. Raised as an
            # `httpx.HTTPError` this would carry no status, so `kind` would
            # derive "network" — which `breaker.TRANSIENT_KINDS` treats as
            # transient, letting a permanent misconfiguration count as an
            # outage and park every book for 24 hours, quietly. A rejected
            # redirect is a misconfiguration and has to stay loud.
            target = resp.headers.get("location")
            raise ClientError(
                self.name,
                f"{what} was redirected"
                + (f" to {target}" if target else " without giving a Location")
                + f" (HTTP {resp.status_code}); refusing to follow it",
                status=resp.status_code,
                kind="auth",
            )
        if resp.status_code >= 400:
            raise ClientError(
                self.name,
                f"{what} failed with HTTP {resp.status_code}{self._detail(resp)}",
                status=resp.status_code,
                retry_after=_retry_after(resp),
                body=resp.text[:300],
            )
        return resp

    def _detail(self, resp: httpx.Response) -> str:
        """The part of an error worth keeping, if any of it is.

        See `quotes_error_bodies`. Where the body is withheld the message says
        so, so an operator reading a terse failure knows the response said more
        and that this was a decision rather than an empty answer.
        """
        if not self.quotes_error_bodies:
            return " (response body withheld: this request carries a credential)"
        snippet = resp.text[:300].replace("\n", " ").strip()
        return f": {snippet}" if snippet else ""

    def _read_bounded(self, streamed: httpx.Response, what: str) -> bytes:
        """The whole body, or a refusal — never a piece of one."""
        chunks: list[bytes] = []
        total = 0
        for chunk in streamed.iter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise ClientError(
                    self.name,
                    f"{what} returned more than "
                    f"{MAX_RESPONSE_BYTES // (1024 * 1024)}MB; refusing to read it",
                    status=streamed.status_code,
                    # A service that answers with something unreadable is
                    # misbehaving, which is what `server` means everywhere else.
                    kind="server",
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def request(self, method: str, path: str, what: str, **kwargs) -> httpx.Response:
        try:
            # Streamed so the body can be bounded as it arrives rather than
            # after it is all in memory, then handed back as an ordinary
            # response so every caller keeps reading it the same way.
            #
            # "Ordinary" has one seam: this object is built by hand rather than
            # by the client, so `elapsed` raises and `http_version` and
            # `extensions` are unset. Nothing in this app reads any of the
            # three — `.status_code`, `.headers`, `.text`, `.json()`, `.cookies`
            # and `.request` all behave exactly as they did, and the response's
            # `content-length` is recomputed from the body.
            with self._client.stream(method, path, **kwargs) as streamed:
                body = self._read_bounded(streamed, what)
                resp = httpx.Response(
                    status_code=streamed.status_code,
                    headers=[
                        (name, value)
                        for name, value in streamed.headers.raw
                        if name.decode("latin-1").lower() not in _TRANSFER_HEADERS
                    ],
                    content=body,
                    request=streamed.request,
                )
        except httpx.TransportError as exc:
            # Never landed, or the connection died on the way. This is the one
            # that really is "could not reach".
            raise ClientError(
                self.name, f"{what} could not reach {self.base_url}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            # It landed and came back unreadable — a body that lies about its
            # own encoding, say. Reported as the service misbehaving rather
            # than as unreachability, so it is not read as "the network" and
            # quietly forgiven.
            raise ClientError(
                self.name,
                f"{what} could not be read from {self.base_url}: {exc}",
                kind="server",
            ) from exc
        return self._check(resp, what)

    def get(self, path: str, what: str, **kwargs) -> httpx.Response:
        return self.request("GET", path, what, **kwargs)

    def post(self, path: str, what: str, **kwargs) -> httpx.Response:
        return self.request("POST", path, what, **kwargs)

    def put(self, path: str, what: str, **kwargs) -> httpx.Response:
        return self.request("PUT", path, what, **kwargs)

    def json(self, method: str, path: str, what: str, **kwargs):
        resp = self.request(method, path, what, **kwargs)
        try:
            return resp.json()
        except ValueError as exc:
            raise ClientError(
                self.name, f"{what} returned non-JSON (HTTP {resp.status_code})"
            ) from exc
