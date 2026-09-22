"""goodreads — Goodreads to-read shelf to a shelved, indexed library."""

from .permissions import lock_umask

# Run once, here, because this package is imported before anything else in
# either entrypoint (`app.main` under uvicorn, and `app.cli`) and therefore
# before anything has had the chance to open a file. See `app/permissions.py`
# for what it covers and what it deliberately leaves alone.
lock_umask()

__version__ = "1.4.0"
