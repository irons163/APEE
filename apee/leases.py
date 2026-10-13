"""Kernel-owned attempt leases, inherited by managed subprocesses."""

import fcntl
from contextlib import contextmanager
from pathlib import Path

from .config import BusyError


@contextmanager
def attempt_lease(run_dir, create=True):
    path = Path(run_dir) / "active.lock"
    with path.open("a+" if create else "r") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BusyError("這次嘗試仍有執行程序；請先讓程序結束，再恢復或重試。") from exc
        yield handle.fileno()
        # Closing is intentional: LOCK_UN would also unlock inherited descriptors
        # still owned by a surviving subprocess after its controller exits.


def lease_state(run_dir):
    try:
        with attempt_lease(run_dir, create=False):
            return "idle"
    except FileNotFoundError:
        return "missing"
    except BusyError:
        return "active"
    except OSError:
        return "unreadable"
