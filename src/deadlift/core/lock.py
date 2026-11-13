"""The Server's lock: one operation at a time on a Server.

Every operation that changes a Server (a deploy, a Rollback, the watchdog, bootstrap) holds it
for its whole run. It's ``flock(2)`` on ``<state>/lock``, the same lock the Bash scripts took
with flock(1), so Python and Bash holders exclude each other while the port is under way.

The lock's fd is close-on-exec and never handed to a child (ADR 0004): a ``docker`` or
``tailscale`` process that outlives the operation, left running in the background, must not keep
the Server locked.
"""

import fcntl
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

WAITING = "Waiting for another operation on this Server to finish"


def lock_path(state: Path) -> Path:
    return state / "lock"


@contextmanager
def server_lock(state: Path, *, say: TextIO | None = None) -> Generator[None]:
    """Holds the Server's lock under ``state`` (/srv/deadlift on a Server) for the ``with``
    block, making ``state`` (root's alone) if need be. When another operation holds it, says
    :data:`WAITING` on stdout (or ``say``), and waits for as long as that takes.
    """
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(
        lock_path(state),
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        if os.get_inheritable(fd):  # pragma: no cover - O_CLOEXEC makes this impossible
            raise RuntimeError("the Server's lock would be inherited by children")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream = sys.stdout if say is None else say
            stream.write(f"{WAITING}\n")
            stream.flush()
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)
