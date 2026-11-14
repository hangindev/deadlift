import fcntl
import io
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from deadlift.core.lock import WAITING, lock_path, server_lock
from deadlift.core.process import Stream, run, scrubbed_env, start


def is_locked(state: Path) -> bool:
    """Whether some open file description holds the Server's lock: a new one can't take it."""
    fd = os.open(lock_path(state), os.O_WRONLY | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def holds_lock_file(pid: int, state: Path) -> bool:
    target = str(lock_path(state))
    fds = Path(f"/proc/{pid}/fd")
    for fd in fds.iterdir():
        try:
            held = str(fd.readlink())
        except FileNotFoundError:
            # Closed between the listing and the readlink: it holds nothing now.
            continue
        if held == target:
            return True
    return False


def test_the_lock_is_held_inside_the_block_and_released_after(tmp_path: Path) -> None:
    state = tmp_path / "srv"
    with server_lock(state):
        assert is_locked(state)
        assert (state.stat().st_mode & 0o777) == 0o700
        assert (lock_path(state).stat().st_mode & 0o777) == 0o600
    assert not is_locked(state)


def test_the_lock_is_released_when_the_block_raises(tmp_path: Path) -> None:
    try:
        with server_lock(tmp_path):
            raise RuntimeError("deploy failed")
    except RuntimeError:
        pass
    assert not is_locked(tmp_path)


def test_a_second_operation_says_it_waits_then_runs(tmp_path: Path) -> None:
    held = threading.Event()
    release = threading.Event()

    def first() -> None:
        with server_lock(tmp_path):
            held.set()
            release.wait(10)

    thread = threading.Thread(target=first)
    thread.start()
    assert held.wait(10)
    said = io.StringIO()
    threading.Timer(0.3, release.set).start()
    started = time.monotonic()
    with server_lock(tmp_path, say=said):
        waited = time.monotonic() - started
    thread.join()
    assert said.getvalue() == f"{WAITING}\n"
    assert waited >= 0.2


def test_the_lock_is_the_one_flock1_takes(tmp_path: Path) -> None:
    # The Bash scripts' `flock` and this lock exclude each other while the port is under way.
    with server_lock(tmp_path):
        done = run(
            ["flock", "--nonblock", str(lock_path(tmp_path)), "true"],
            env=scrubbed_env(),
        )
    assert done.code == 1


def test_a_detached_child_does_not_hold_the_lock(tmp_path: Path) -> None:
    # As a `docker` or `tailscale` left running in the background would be: a child in its own
    # session, and a grandchild its shell forked off, both still running after the operation.
    env = scrubbed_env()
    with server_lock(tmp_path):
        child = start(["sleep", "60"], env=env, new_session=True, stdout=Stream.DEVNULL)
        forked = run(
            ["/bin/sh", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"],
            env=env,
            stdout=Stream.CAPTURE,
        )
        grandchild = int(forked.stdout)
    try:
        assert not holds_lock_file(child.pid, tmp_path)
        assert not holds_lock_file(grandchild, tmp_path)
        assert not is_locked(tmp_path)
    finally:
        child.kill()
        child.wait()
        os.kill(grandchild, signal.SIGKILL)


def test_the_detached_child_check_would_see_a_leak(tmp_path: Path) -> None:
    # The control for the test above: a child handed the lock's fd keeps the Server locked.
    tmp_path.mkdir(exist_ok=True)
    fd = os.open(lock_path(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    child = subprocess.Popen(["sleep", "60"], pass_fds=(fd,), start_new_session=True)
    os.close(fd)
    try:
        assert holds_lock_file(child.pid, tmp_path)
        assert is_locked(tmp_path)
    finally:
        child.kill()
        child.wait()
