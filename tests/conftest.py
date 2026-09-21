"""Test fixtures.

`app.config.settings` and `app.db.db()` are module-level singletons built at
import time, so `DATA_DIR` and the service URLs have to be set *before*
`app.config` is first imported — which is why the environment is set up at the
top of this file rather than in a fixture. Import order is the whole trick: a
test that imports `app` before this file runs would write to `/data`.

Everything runs against a throwaway directory, and every service URL points at
a port nothing listens on, so a test that accidentally reaches the network
fails fast instead of touching the operator's real Shelfmark.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="goodreads-tests-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ["GOODREADS_SECRET_KEY"] = "test-secret-not-a-real-key"
os.environ["BOOKS_ROOT"] = str(_TMP / "books")
os.environ["AUDIOBOOKS_ROOT"] = str(_TMP / "audiobooks")
os.environ["GOODREADS_USER_ID"] = ""
for _name in (
    "SHELFMARK_URL", "KAVITA_URL", "BOOKLORE_URL", "GRIMMORY_URL",
    "ABS_URL", "OPENNOTEBOOK_URL", "SABNZBD_URL",
):
    os.environ[_name] = "http://127.0.0.1:9"   # discard port: refuses instantly

import pytest  # noqa: E402

from app import breaker  # noqa: E402
from app.db import db  # noqa: E402

#: Order matters only in that `books` may cascade into `stage_runs`.
_TABLES = (
    "stage_runs", "books", "events", "service_breaker", "service_health",
    "settings", "service_map", "credentials", "users",
)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Leave nothing behind: the whole point is a throwaway DATA_DIR."""
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_db():
    """Empty database for every test, whatever order they run in."""
    for table in _TABLES:
        db().execute(f"DELETE FROM {table}")
    yield


class FakeClock:
    """A hand-driven clock for `breaker`.

    Starts at real now so that stamps the *database* writes (which use the
    real clock) and stamps the breaker writes stay comparable — a fake epoch
    in 1970 would make every `transient_since >= opened_at` comparison
    meaningless.
    """

    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def iso(self) -> str:
        return datetime.fromtimestamp(self.now, timezone.utc).isoformat(
            timespec="seconds"
        )


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(breaker, "_clock", fake)
    return fake
