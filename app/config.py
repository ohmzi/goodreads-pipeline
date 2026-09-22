"""Runtime configuration.

Service URLs default to container names on the shared docker networks, never
`localhost:<published-port>` — ufw's default-deny blocks container -> host
published ports on this box, so a localhost URL resolves to the container
itself and fails in a way that looks like the service is down.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

APP_DIR = Path(__file__).resolve().parent
BASE_DIR = APP_DIR.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass
class Settings:
    # --- identity -------------------------------------------------------
    secret_key: str = field(default_factory=lambda: _env("GOODREADS_SECRET_KEY"))
    goodreads_user_id: str = field(default_factory=lambda: _env("GOODREADS_USER_ID"))

    # --- storage --------------------------------------------------------
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "/data")))
    books_root: Path = field(default_factory=lambda: Path(_env("BOOKS_ROOT", "/books")))
    audiobooks_root: Path = field(
        default_factory=lambda: Path(_env("AUDIOBOOKS_ROOT", "/audiobooks"))
    )
    # Where Shelfmark drops finished downloads, relative to books_root.
    staging_dirname: str = field(
        default_factory=lambda: _env("STAGING_DIRNAME", "newDownloads")
    )
    # Open Notebook sees the same tree under a different mount point. Paths
    # handed to its API must be expressed in *its* namespace.
    opennotebook_library_root: str = field(
        default_factory=lambda: _env(
            "OPENNOTEBOOK_LIBRARY_ROOT", "/app/data/uploads/library"
        )
    )
    # Audiobookshelf is the one app that does NOT mount the library at a
    # neutral path — it mounts the host directory at its own full path. So its
    # view of a file differs from ours and paths must be translated before
    # asking it which library holds a book.
    # Deployment-specific: this must be the path Audiobookshelf itself sees the
    # audiobooks library at, which differs on every host. Set it in
    # docker-compose.override.yml or .env. The default is only a placeholder.
    abs_library_root: str = field(
        default_factory=lambda: _env(
            "ABS_LIBRARY_ROOT", "/library/Audiobooks"
        )
    )

    # --- behaviour ------------------------------------------------------
    min_free_space_gb: int = field(default_factory=lambda: _env_int("MIN_FREE_SPACE_GB", 50))
    poll_interval: int = field(default_factory=lambda: _env_int("POLL_INTERVAL", 15))
    bind: str = field(default_factory=lambda: _env("BIND", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8090))
    auto_shelve_default: bool = field(
        default_factory=lambda: _env("AUTO_SHELVE_DEFAULT", "1") == "1"
    )
    # Set COOKIE_SECURE=1 once goodreads is only ever reached over HTTPS.
    # Leaving it on while serving plain HTTP makes the browser drop the
    # session cookie and login appears to silently fail.
    cookie_secure: bool = field(
        default_factory=lambda: _env("COOKIE_SECURE", "0") == "1"
    )
    # X-Forwarded-For is client-controlled, so trusting it for rate limiting
    # lets an attacker rotate the header and bypass the per-client limit.
    # Only enable this when a reverse proxy you control sets the header.
    trust_proxy: bool = field(
        default_factory=lambda: _env("TRUST_PROXY", "0") == "1"
    )
    # What this app is reached as, when that differs from the Host header a
    # request arrives with. The same-origin check on state-changing requests
    # and on the VNC handshake compares a request's `Origin` against its own
    # `Host`, which is correct whenever nothing rewrites it. A reverse proxy
    # that does rewrite Host — terminating TLS on a public name and forwarding
    # to `goodreads:8090`, say — makes the two disagree, and without this the
    # VNC panel silently stops connecting. Set it to the origin a browser
    # actually uses, scheme and port included: `https://goodreads.example.com`.
    public_origin: str = field(default_factory=lambda: _env("PUBLIC_ORIGIN"))

    # --- service endpoints ---------------------------------------------
    shelfmark_url: str = field(
        default_factory=lambda: _env("SHELFMARK_URL", "http://shelfmark:8084")
    )
    kavita_url: str = field(default_factory=lambda: _env("KAVITA_URL", "http://kavita:5000"))
    booklore_url: str = field(
        default_factory=lambda: _env("BOOKLORE_URL", "http://booklore:6060")
    )
    grimmory_url: str = field(
        default_factory=lambda: _env("GRIMMORY_URL", "http://grimmory:6060")
    )
    abs_url: str = field(
        default_factory=lambda: _env("ABS_URL", "http://audiobookshelf:80")
    )
    opennotebook_url: str = field(
        default_factory=lambda: _env("OPENNOTEBOOK_URL", "http://open_notebook:5055")
    )
    sabnzbd_url: str = field(
        default_factory=lambda: _env("SABNZBD_URL", "http://sabnzbd:8080")
    )

    # --- derived --------------------------------------------------------
    @property
    def staging_dir(self) -> Path:
        return self.books_root / self.staging_dirname

    @property
    def db_path(self) -> Path:
        return self.data_dir / "goodreads.db"

    @property
    def categories_path(self) -> Path:
        # categories.yml ships in the image but is overridable from /data so
        # tuning survives a rebuild.
        override = self.data_dir / "categories.yml"
        return override if override.exists() else APP_DIR / "categories.yml"

    def load_categories(self) -> dict:
        with open(self.categories_path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)


settings = Settings()
