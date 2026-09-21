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
                 kind: str | None = None, retry_after: float | None = None):
        self.service = service
        self.status = status
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

    def __init__(self, base_url: str | None = None):
        if base_url:
            self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url, timeout=DEFAULT_TIMEOUT, follow_redirects=True
        )

    def __enter__(self) -> "ServiceClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- request helpers -------------------------------------------------
    def _check(self, resp: httpx.Response, what: str) -> httpx.Response:
        if resp.status_code >= 400:
            snippet = resp.text[:300].replace("\n", " ").strip()
            raise ClientError(
                self.name,
                f"{what} failed with HTTP {resp.status_code}: {snippet}",
                status=resp.status_code,
                retry_after=_retry_after(resp),
            )
        return resp

    def request(self, method: str, path: str, what: str, **kwargs) -> httpx.Response:
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ClientError(self.name, f"{what} could not reach {self.base_url}: {exc}") from exc
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
