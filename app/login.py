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
