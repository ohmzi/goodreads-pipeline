"""On-disk permissions for the data volume.

The data volume is the secret store. `goodreads.db` holds every encrypted
credential and its `-wal`/`-shm` sidecars hold the same rows in the clear,
`goodreads_state.json` is a live Goodreads session, and `browser-profile/` is a
Chromium profile carrying the same cookies. None of it is meant to be readable
by another account on the host — and all of it was, because the process ran
under the usual `022` umask, which makes everything it creates world-readable.

Two mechanisms, because neither is enough on its own:

  * `lock_umask()` is called once while the `app` package is imported, before
    anything has opened a file, so every file the process *creates* is
    owner-only without any call site having to remember. It is also inherited
    by the Chromium child, which is how the browser profile is covered.
  * `harden_data_dir()` fixes what is already on disk. A umask only affects
    creation, and a deployment that has been running for a while already has a
    database, its sidecars and a session file at whatever mode they were made
    with — changing the umask alone would leave every one of them as it was.

The media library is deliberately not covered, and the umask is not allowed to
decide it either: `/books` and `/audiobooks` exist to be read by other
applications running as their own uid, so a category folder created owner-only
would make every book beneath it unindexable. See `pathing.LIBRARY_DIR_MODE`.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Owner-only. See the module docstring.
LOCKED_UMASK = 0o077

#: Directories under the data dir that must not be traversable by anyone else.
_OWNER_ONLY_DIRS = ("browser-profile",)


def lock_umask() -> None:
    """Set the process umask for the data volume's sake. Idempotent."""
    os.umask(LOCKED_UMASK)


def harden_data_dir(data_dir: Path) -> list[str]:
    """Tighten what is already there, and report what changed.

    Idempotent, and best-effort by design: a file that is not there is not a
    problem, and a `chmod` that fails is not worth refusing to start over —
    losing the service to a permissions error would be a worse outcome than the
    permissions themselves.

    The database is matched by prefix rather than by name so the `-wal` and
    `-shm` sidecars are covered by the same rule that covers the database.
    Those are not incidental: SQLite writes committed rows into the WAL before
    checkpointing, so the sidecar holds the same credential ciphertext the
    database does.
    """
    changed: list[str] = []
    root = Path(data_dir)

    for path in sorted(root.glob("goodreads.db*")):
        if path.is_file() and _tighten(path, 0o600):
            changed.append(path.name)
    state = root / "goodreads_state.json"
    if state.is_file() and _tighten(state, 0o600):
        changed.append(state.name)

    for name in _OWNER_ONLY_DIRS:
        directory = root / name
        if directory.is_dir() and _tighten(directory, 0o700):
            changed.append(name + "/")

    return changed


def _tighten(path: Path, mode: int) -> bool:
    """Set `mode`, and say whether it was actually different."""
    try:
        if (path.stat().st_mode & 0o777) == mode:
            return False
        os.chmod(path, mode)
        return True
    except OSError:
        return False
