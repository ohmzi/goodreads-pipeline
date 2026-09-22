"""Interactive Goodreads login, driven from the browser.

There is no password flow to automate (see `goodreads.py`), so a human has to
complete the Amazon sign-in once. This module owns a real Chromium on a virtual
display; the UI embeds that display over noVNC so the login happens inside
our own page and the captured session lands on the volume.

The browser runs on its own thread with a persistent context, because
Playwright's sync API is not thread-safe and the login outlives a single HTTP
request by design — you might take two minutes over a CAPTCHA.
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from .config import settings
from .goodreads import state_path

DISPLAY = ":99"
SIGN_IN_URL = "https://www.goodreads.com/user/sign_in"


class LoginSession:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started_at: float | None = None
        self._message = "idle"
        self._active = False
        self._save_requested = False

    # -- state -----------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return {
                "active": self._active,
                "started_at": self._started_at,
                "message": self._message,
                "saved": state_path().exists(),
                "vnc_url": "/vnc/vnc.html?autoconnect=1&resize=scale",
            }

    def _set(self, message: str, active: bool | None = None) -> None:
        with self._lock:
            self._message = message
            if active is not None:
                self._active = active

    # -- lifecycle -------------------------------------------------------
    def start(self) -> dict:
        with self._lock:
            if self._active:
                return {"ok": True, "message": "already running"}
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="goodreads-login", daemon=True)
        self._thread.start()
        return {"ok": True, "message": "browser starting"}

    def save(self) -> dict:
        """Ask the running browser to dump its session, then close."""
        if not self._active:
            return {"ok": False, "message": "no login in progress"}
        self._save_requested = True
        self._stop.set()
        return {"ok": True, "message": "saving session"}

    def cancel(self) -> dict:
        self._stop.set()
        self._set("cancelled", active=False)
        return {"ok": True}

    # -- the browser thread ----------------------------------------------
    def _run(self) -> None:
        self._save_requested = False
        self._started_at = time.time()
        self._set("launching browser", active=True)

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - image ships playwright
            self._set(f"playwright unavailable: {exc}", active=False)
            return

        profile_dir = Path(settings.data_dir) / "browser-profile"
        profile_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as pw:
            try:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=False,
                    viewport={"width": 1280, "height": 900},
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                    env={"DISPLAY": DISPLAY},
                )
            except Exception as exc:  # noqa: BLE001
                self._set(f"could not launch browser: {exc}", active=False)
                return

            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(SIGN_IN_URL, wait_until="domcontentloaded", timeout=60_000)
                self._set("log in, then press Save session", active=True)

                # Hold the browser open until Save (or Cancel) is pressed.
                while not self._stop.is_set():
                    time.sleep(1)

                if self._save_requested:
                    context.storage_state(path=str(state_path()))
                    self._set("session saved", active=False)
                else:
                    self._set("cancelled", active=False)
            except Exception as exc:  # noqa: BLE001
                self._set(f"login window error: {exc}", active=False)
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass


login_session = LoginSession()


def delete_stored_session() -> None:
    path = state_path()
    if path.exists():
        path.unlink()


def _login_browser_is_running() -> bool:
    """Whether a Chromium is up on this machine's login profile.

    Asked of the process table as well as of `login_session`, because the two
    ways to reach `forget_stored_session` are different processes. The server
    owns the browser and its in-memory `_active` marker; `forget-session` is a
    CLI invocation, and in a fresh process that marker is *always* False — so a
    guard that consulted only `_active` would never fire for the one caller
    that can actually delete the profile out from under a running Chromium.

    Any process whose command line names the profile directory counts, which is
    how Playwright passes it (`--user-data-dir=`). Reading /proc needs no
    external binary, so this works on a bare-metal host as well as in the
    image.
    """
    if login_session.status()["active"]:
        return True

    marker = str(Path(settings.data_dir) / "browser-profile").encode()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if marker in (entry / "cmdline").read_bytes():
                return True
        except OSError:
            continue
    return False


def forget_stored_session() -> dict:
    """Delete everything that carries the Goodreads session.

    `delete_stored_session` removes the state file, and that is not on its own
    enough to answer "forget this session": the persistent browser profile
    holds the same cookies for the same account, so removing one of the two
    leaves a live session sitting on the volume. Both go, and the next sign-in
    starts from nothing.

    Refuses while a login browser is running. It is using that profile, and
    deleting it underneath Chromium would leave the operator with a broken
    window *and* no session — neither the one they wanted to keep nor the one
    they wanted to drop. See `_login_browser_is_running` for why that check
    cannot be the in-memory flag alone.

    Reports `ok: False` rather than raising when something cannot be removed:
    the caller is a CLI whose whole job is to say whether the cookies are gone,
    so a traceback would be a worse answer than an honest no.
    """
    if _login_browser_is_running():
        return {
            "ok": False,
            "removed": [],
            "message": "a login browser is running — cancel it first",
        }

    removed: list[str] = []
    state = state_path()
    try:
        if state.exists():
            delete_stored_session()
            removed.append(state.name)
    except OSError as exc:
        return {
            "ok": False,
            "removed": removed,
            "message": f"could not remove {state.name}: {exc}",
        }

    profile = Path(settings.data_dir) / "browser-profile"
    if profile.is_dir():
        try:
            shutil.rmtree(profile)
        except OSError as exc:
            return {
                "ok": False,
                "removed": removed,
                "message": f"could not remove {profile.name}/: {exc}",
            }
        removed.append(profile.name + "/")

    return {"ok": True, "removed": removed}
