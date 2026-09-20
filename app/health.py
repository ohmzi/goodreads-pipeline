"""Continuous health checks for the services goodreads depends on.

Credential failures used to be invisible until a book happened to hit the
affected stage — which could be hours later, and then appeared as an opaque
stage error rather than "Audiobookshelf is rejecting your password". Probing
each service on a timer turns that into something the UI can put in front of
you immediately.

Each probe is deliberately the *cheapest authenticated call* the service
offers, so the check proves the credentials work rather than merely proving the
host is up. A service whose health endpoint is unauthenticated would happily
report green with a wrong API key, which is the exact failure this exists to
catch.
"""

from __future__ import annotations

from .clients.abs_client import AudiobookshelfClient
from .clients.base import ClientError, ServiceClient
from .clients.booklore import BookLoreClient, GrimmoryClient
from .clients.kavita import KavitaClient
from .clients.opennotebook import OpenNotebookClient
from .clients.shelfmark import ShelfmarkClient
from .db import db

#: service name -> (label, client factory, human name for messages)
SERVICES: list[tuple[str, str, type[ServiceClient]]] = [
    ("shelfmark", "Shelfmark", ShelfmarkClient),
    ("kavita", "Kavita", KavitaClient),
    ("booklore", "BookLore", BookLoreClient),
    ("grimmory", "Grimmory", GrimmoryClient),
    ("audiobookshelf", "Audiobookshelf", AudiobookshelfClient),
    ("opennotebook", "Open Notebook", OpenNotebookClient),
]

#: What each service cannot do when it is down, so the UI can say what breaks.
IMPACT = {
    "shelfmark": "no downloads can start",
    "kavita": "books are not indexed in Kavita",
    "booklore": "books are not indexed in BookLore",
    "grimmory": "books are not indexed in Grimmory",
    "audiobookshelf": "audiobooks are not indexed",
    "opennotebook": "books are not added to notebooks",
}

#: Which per-book stages each service is on the hook for. Used to say, on a
#: service's own page, what is currently stuck waiting on it. Named
#: `SERVICE_STAGES` so it never reads as `models.STAGES`, which is the pipeline
#: order and a different thing entirely.
SERVICE_STAGES = {
    "shelfmark": ("acquire_ebook", "acquire_audiobook"),
    "kavita": ("index", "verify"),
    "booklore": ("index", "verify"),
    "grimmory": ("index", "verify"),
    "audiobookshelf": ("index", "verify"),
    "opennotebook": ("notebook", "verify"),
    "goodreads": ("shelve",),
}


def canonical(name: str) -> str:
    """Normalise a service name to the slug used in `SERVICES`.

    Two vocabularies reach the database and they do not agree: the client
    classes name themselves in prose ("open notebook", from
    `clients/opennotebook.py`) while this registry uses a slug
    ("opennotebook"). `stage_runs.service` is written from the former and
    `events.service` from the latter, so anything that joins or compares them
    has to normalise first rather than hoping they match.
    """
    return (name or "").strip().lower().replace(" ", "")


#: Canonical name -> the human label, for anything that needs to render one.
#: The two pseudo-services are included because they really do appear in
#: `stage_runs.service`: a missing credential is reported by `require_cred` as
#: "settings", and Goodreads is not in `SERVICES` because it is not probed
#: like a library app — it has a browser session instead.
LABELS: dict[str, str] = {name: label for name, label, _ in SERVICES}
LABELS["goodreads"] = "Goodreads"
LABELS["settings"] = "Settings"


def label_for(name: str) -> str:
    """Human label for a service name in either vocabulary."""
    key = canonical(name)
    return LABELS.get(key, key or "unknown")


def probe(name: str, factory: type[ServiceClient]) -> tuple[bool, str, str]:
    """(ok, detail, failure_kind) for one service.

    Calls something that requires authentication wherever one exists — a
    health endpoint that answers without credentials would pass even with a
    wrong key, which defeats the purpose.
    """
    client = factory()
    try:
        if name == "shelfmark":
            # No auth on this instance; the health route is the honest check.
            detail = client.health()
        elif name == "opennotebook":
            # Listing notebooks is authenticated and cheap.
            count = len(client.notebooks())
            detail = f"{count} notebook(s)"
        else:
            # For every library app, listing libraries proves the token works.
            count = len(client.libraries())
            detail = f"{count} libraries"
        return True, detail, ""
    except ClientError as exc:
        return False, str(exc), exc.kind
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        return False, f"{type(exc).__name__}: {exc}", "network"
    finally:
        client.close()


def check_all(quiet: bool = False) -> dict:
    """Probe every service and record the result. Returns a summary."""
    results: dict[str, dict] = {}
    newly_broken: list[str] = []
    recovered: list[str] = []

    for name, label, factory in SERVICES:
        previous = db().query_one(
            "SELECT ok FROM service_health WHERE service = ?", (name,)
        )
        was_ok = bool(previous["ok"]) if previous else None

        ok, detail, kind = probe(name, factory)
        db().set_service_health(name, ok, detail, kind)
        results[name] = {"ok": ok, "detail": detail, "failure_kind": kind}

        if not ok and was_ok is not False:
            newly_broken.append(name)
            db().log(
                f"{label} is unreachable or rejecting credentials: {detail}",
                level="error",
                stage="health",
                service=name,
            )
        elif ok and was_ok is False:
            recovered.append(name)
            db().log(f"{label} is healthy again", stage="health", service=name)

    if newly_broken and not quiet:
        db().log(
            "services needing attention: " + ", ".join(newly_broken), level="error"
        )
    return {"results": results, "newly_broken": newly_broken, "recovered": recovered}


def summary() -> dict:
    """Current health for the UI, including anything never checked."""
    stored = {row["service"]: row for row in db().service_health()}
    services = []
    for name, label, _factory in SERVICES:
        row = stored.get(name)
        services.append(
            {
                "service": name,
                "label": label,
                "impact": IMPACT.get(name, ""),
                "checked": row is not None,
                "ok": bool(row["ok"]) if row else None,
                "detail": (row or {}).get("detail", "never checked"),
                "failure_kind": (row or {}).get("failure_kind", ""),
                "checked_at": (row or {}).get("checked_at"),
                "ok_since": (row or {}).get("ok_since"),
            }
        )
    unhealthy = [s for s in services if s["ok"] is False]
    return {
        "services": services,
        "unhealthy": unhealthy,
        "auth_failures": [s for s in unhealthy if s["failure_kind"] == "auth"],
        "all_ok": bool(services) and not unhealthy,
    }
