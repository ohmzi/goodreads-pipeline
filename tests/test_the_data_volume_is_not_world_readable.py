"""What the data volume looks like to another account on the host.

The data volume is the secret store: the database holds every encrypted
credential and its WAL holds the same rows, the state file is a live Goodreads
session, and the browser profile carries the same cookies. All of it was
readable by anyone, because the process ran under the usual `022` umask.

Two mechanisms, and these tests cover both, because neither alone is enough: a
umask only affects what is *created*, and a deployment that has been running
already has the files.
"""

from __future__ import annotations

import os
import stat

import pytest

from app import permissions


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_the_process_umask_is_owner_only():
    """Set at `app` package import, so neither entrypoint can miss it.

    The suite imports the package (conftest does), so the live value is the one
    the app set — read it by setting it and putting the old one back.
    """
    assert permissions.LOCKED_UMASK == 0o077

    previous = os.umask(LOCKED_PROBE := 0o077)
    os.umask(previous)

    assert previous == LOCKED_PROBE, "importing `app` did not set the umask"


def test_a_file_created_now_is_owner_only(tmp_path):
    """Read through the same umask the app runs under, not by asserting the
    constant — the point is what the filesystem does with it."""
    previous = os.umask(permissions.LOCKED_UMASK)
    try:
        target = tmp_path / "created-with-the-app-umask"
        target.write_text("x")
    finally:
        os.umask(previous)

    assert _mode(target) == 0o600


def test_a_stored_database_is_tightened_with_its_sidecars(tmp_path):
    """The sidecars are not incidental: SQLite commits into the WAL before
    checkpointing, so it holds the same credential ciphertext."""
    for name in ("goodreads.db", "goodreads.db-wal", "goodreads.db-shm"):
        (tmp_path / name).write_text("x")
        os.chmod(tmp_path / name, 0o644)

    changed = permissions.harden_data_dir(tmp_path)

    assert sorted(changed) == ["goodreads.db", "goodreads.db-shm", "goodreads.db-wal"]
    for name in ("goodreads.db", "goodreads.db-wal", "goodreads.db-shm"):
        assert _mode(tmp_path / name) == 0o600


def test_the_session_file_is_tightened(tmp_path):
    state = tmp_path / "goodreads_state.json"
    state.write_text("{}")
    os.chmod(state, 0o644)

    assert permissions.harden_data_dir(tmp_path) == ["goodreads_state.json"]
    assert _mode(state) == 0o600


def test_the_browser_profile_is_made_untraversable(tmp_path):
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    os.chmod(profile, 0o755)

    assert permissions.harden_data_dir(tmp_path) == ["browser-profile/"]
    assert _mode(profile) == 0o700


def test_nothing_present_is_not_an_error(tmp_path):
    """A fresh deployment has none of these yet, and must still start."""
    assert permissions.harden_data_dir(tmp_path) == []


def test_a_second_run_changes_nothing(tmp_path):
    """It runs on every start, so it has to be idempotent — otherwise the log
    line it writes would appear on every restart forever.

    The file is chmodded to 0644 first on purpose: under the app's own umask a
    freshly written file is already 0600, and the case this whole function
    exists for is a file written *before* the umask changed.
    """
    database = tmp_path / "goodreads.db"
    database.write_text("x")
    os.chmod(database, 0o644)

    assert permissions.harden_data_dir(tmp_path) == ["goodreads.db"]
    assert permissions.harden_data_dir(tmp_path) == []


def test_it_reports_what_it_changed_and_never_raises(tmp_path, monkeypatch):
    """Best effort by design: losing the service to a permissions error would
    be a worse outcome than the permissions themselves."""
    (tmp_path / "goodreads.db").write_text("x")

    def boom(*_args, **_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(permissions.os, "chmod", boom)

    assert permissions.harden_data_dir(tmp_path) == []


# --------------------------------------------------------------------------
# The library is the other case, and deliberately not covered by any of this
# --------------------------------------------------------------------------
def test_a_created_library_directory_stays_readable_by_other_apps():
    """`umask 077` would make it 0700, and a book inside a folder no other
    container can traverse is a book nothing indexes — silently, because this
    app can still read its own tree.

    0o755 rather than 0o777 because it is what a directory had before the umask
    changed (`0o777 & ~0o022`): nothing about the library's accessibility moves.
    """
    from app import pathing

    assert pathing.LIBRARY_DIR_MODE == 0o755
    assert pathing.LIBRARY_DIR_MODE & 0o055 == 0o055, "not traversable by others"
    assert not pathing.LIBRARY_DIR_MODE & 0o022, "writable by others"


def test_every_missing_level_is_corrected_not_just_the_leaf(tmp_path):
    """`mkdir(parents=True)` creates the whole chain — category, then author —
    and the level above the destination is as load-bearing as the destination:
    a readable folder inside an unreadable one is a folder nothing can reach.
    """
    from app import pathing

    deep = tmp_path / "Fiction" / "Some Author - A Title (1997)"

    previous = os.umask(permissions.LOCKED_UMASK)  # what the app runs under
    try:
        pathing.makedirs_for_library(deep)
    finally:
        os.umask(previous)

    assert _mode(deep) == pathing.LIBRARY_DIR_MODE
    assert _mode(deep.parent) == pathing.LIBRARY_DIR_MODE, "left owner-only"


def test_an_existing_directory_keeps_its_own_mode(tmp_path):
    """A folder the operator set up deliberately is not this code's to change."""
    from app import pathing

    existing = tmp_path / "Fiction"
    existing.mkdir()
    os.chmod(existing, 0o700)
    leaf = existing / "Author - Title"

    pathing.makedirs_for_library(leaf)

    assert _mode(existing) == 0o700
    assert leaf.is_dir()


def test_every_place_that_makes_a_library_directory_uses_the_helper():
    """Both of them, because the umask breaks whichever one is missed.

    `move_into_place` is the one on the pipeline's path; `cli rename --apply` is
    the one an operator runs by hand, and it was missed until an adversarial
    read of this diff found it — a new author folder created `0700` there makes
    every audiobook moved into it invisible to Audiobookshelf, silently,
    because this process can still read its own tree.
    """
    import inspect

    from app import cli, pathing

    assert "makedirs_for_library(" in inspect.getsource(pathing.move_into_place)
    assert "makedirs_for_library(" in inspect.getsource(cli.cmd_rename)
    # And no bare mkdir left in either, which is what the miss looked like.
    assert ".mkdir(" not in inspect.getsource(cli.cmd_rename)
    assert ".mkdir(" not in inspect.getsource(pathing.move_into_place)


def test_rename_apply_leaves_a_new_author_folder_traversable(tmp_path, monkeypatch):
    """The end the operator sees: a real `rename --apply` run.

    The folder it creates has to be readable by the other containers, so this
    runs under the app's own umask and asserts the mode rather than asserting
    that a helper was called.
    """
    import argparse

    from app import cli

    root = tmp_path / "audiobooks"
    root.mkdir()
    (root / "Andy Weir - Project Hail Mary.mp3").write_bytes(b"x" * 200_000)
    monkeypatch.setattr(cli.settings, "audiobooks_root", root)

    previous = os.umask(permissions.LOCKED_UMASK)
    try:
        assert cli.cmd_rename(argparse.Namespace(apply=True)) == 0
    finally:
        os.umask(previous)

    created = root / "Andy Weir"
    assert created.is_dir(), "nothing was moved, so nothing was tested"
    assert _mode(created) == 0o755, "the new author folder is not traversable"


# --------------------------------------------------------------------------
# Forgetting the Goodreads session
# --------------------------------------------------------------------------
def test_forgetting_removes_the_state_file_and_the_profile(tmp_path, monkeypatch):
    """Both, because they carry the same cookies — removing one of the two
    leaves a live session for a real account sitting on the volume, which is
    the opposite of what was asked for."""
    from app import login

    state = tmp_path / "goodreads_state.json"
    state.write_text("{}")
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    (profile / "Cookies").write_text("cookie")

    monkeypatch.setattr(login, "state_path", lambda: state)
    monkeypatch.setattr(login.settings, "data_dir", tmp_path)

    result = login.forget_stored_session()

    assert result["ok"] is True
    assert sorted(result["removed"]) == ["browser-profile/", "goodreads_state.json"]
    assert not state.exists()
    assert not profile.exists()


def test_forgetting_refuses_when_a_browser_is_running_for_real(tmp_path, monkeypatch):
    """The cross-process case, which is the only one the CLI ever hits.

    `login_session._active` is in-memory state belonging to the *server*, and
    `forget-session` is a separate process where it is always False — so a
    guard reading only that flag would rmtree the profile out from under live
    Chromium, which is the exact outcome it exists to prevent. The check has to
    look at the process table.
    """
    from app import login

    state = tmp_path / "goodreads_state.json"
    state.write_text("{}")
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    monkeypatch.setattr(login, "state_path", lambda: state)
    monkeypatch.setattr(login.settings, "data_dir", tmp_path)
    # Not the in-memory flag — a process that names the profile directory.
    monkeypatch.setattr(login, "_login_browser_is_running", lambda: True)

    result = login.forget_stored_session()

    assert result["ok"] is False
    assert state.exists() and profile.exists()


def test_the_browser_check_reads_the_process_table(monkeypatch):
    """Anchored on what it actually inspects: a missing `Origin`-style escape
    hatch here would silently disable the guard in the CLI."""
    import inspect

    from app import login

    source = inspect.getsource(login._login_browser_is_running)
    assert "/proc" in source, "it does not consult the process table"
    assert "cmdline" in source, "it does not read command lines"
    assert "login_session.status()" in source, "the in-process case was dropped"


def test_forgetting_refuses_while_a_login_browser_is_running(tmp_path, monkeypatch):
    """Deleting the profile underneath Chromium would leave the operator with a
    broken window and no session either way."""
    from app import login

    state = tmp_path / "goodreads_state.json"
    state.write_text("{}")
    monkeypatch.setattr(login, "state_path", lambda: state)
    monkeypatch.setattr(login.settings, "data_dir", tmp_path)
    monkeypatch.setattr(login.login_session, "_active", True)

    result = login.forget_stored_session()

    assert result["ok"] is False
    assert "cancel it first" in result["message"]
    assert state.exists(), "it went ahead anyway"


def test_forgetting_nothing_is_not_an_error(tmp_path, monkeypatch):
    from app import login

    monkeypatch.setattr(login, "state_path", lambda: tmp_path / "absent.json")
    monkeypatch.setattr(login.settings, "data_dir", tmp_path)

    assert login.forget_stored_session() == {"ok": True, "removed": []}
