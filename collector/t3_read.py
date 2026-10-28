"""Bounded, serialized read access to provider-owned T3 SQLite state."""
import fcntl
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path

READ_SECONDS = 15
SNAPSHOT_FILES = {"statev2.sqlite", "statev2.sqlite-wal", "statev2.sqlite-shm"}


def _remove_snapshot(directory):
    if not directory.exists() and not directory.is_symlink():
        return
    if directory.is_symlink() or not directory.is_dir():
        raise OSError("Unsafe T3 snapshot directory")
    entries = list(directory.iterdir())
    if any(item.name not in SNAPSHOT_FILES or not stat.S_ISREG(item.lstat().st_mode) for item in entries):
        raise OSError("Unexpected file in T3 snapshot directory")
    for item in entries:
        item.unlink()
    directory.rmdir()


def _transaction(database, deadline):
    if time.monotonic() >= deadline:
        raise sqlite3.OperationalError("T3 read deadline exceeded")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
        connection.execute("BEGIN")
        # Establish one SQLite snapshot before the caller issues multiple queries.
        connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return connection
    except BaseException:
        connection.close()
        raise


@contextmanager
def read_t3_database(home):
    """Read provider state in place. Never copy a database or its sidecars.

    Missing WAL sidecars fail closed before SQLite can create provider files.
    The lock serializes readers. Cleanup removes only an old, known snapshot.
    """
    home = Path(home).resolve()
    database = home / ".t3/userdata/statev2.sqlite"
    cache = home / ".cache/lighthouse"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    if cache.is_symlink():
        raise OSError("Unsafe Lighthouse cache directory")
    descriptor = os.open(cache / "t3-read.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        snapshot = cache / "t3-snapshot"
        _remove_snapshot(snapshot)
        connection = None
        deadline = time.monotonic() + READ_SECONDS
        try:
            with database.open("rb") as source:
                header = source.read(20)
            wal_mode = header[18:20] == b"\x02\x02"
            sidecars_present = all(Path(str(database) + suffix).is_file() for suffix in ("-wal", "-shm"))
            if wal_mode and not sidecars_present:
                raise sqlite3.OperationalError(
                    "T3 WAL sidecars are missing; database copying is disabled")
            connection = _transaction(database, deadline)
            yield connection
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                _remove_snapshot(snapshot)
