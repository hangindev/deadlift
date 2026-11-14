"""`deadlift server bootstrap <server>`: turns <server>, a fresh Ubuntu 24.04 machine or a Server
already, into an up to date Server. It sends this checkout's Server code (the Python packages
deadlift.core, deadlift.server and deadlift.bootstrap, and, while the port to Python is under way,
the scripts still in server/) over SSH as root, and runs deadlift.bootstrap.bootstrap there (see
it for what it sets up). Safe to run again: a run with nothing to do changes nothing. <server> is
anything `ssh root@` takes: the machine's public address before it joins the tailnet, its tailnet
name after.

Exit codes: 0 done, 1 failed (or couldn't reach the Server), 2 refused or bad usage (nothing was
changed). Otherwise, bootstrap's own exit code on the Server.

Standard library only.
"""

import io
import os
import re
import sys
import tarfile
from collections.abc import Sequence
from pathlib import Path

from deadlift.core.outcome import Exit, reason
from deadlift.core.process import Stream, scrubbed_env, start

# This checkout: src/deadlift/cli/server_bootstrap.py is three directories down.
CHECKOUT = Path(__file__).resolve().parents[3]
# The packages a Server needs to bootstrap, relative to src/deadlift.
SENT_PACKAGES = ("core", "server", "bootstrap")
SSH_UNREACHABLE = 255

USAGE = "Usage: deadlift server bootstrap <server>\n"

# Run by root's shell on the Server, with the tar on stdin: unpacked into a directory only root
# can read, bootstrapped from, and removed. Commands there read nothing from the maintainer's
# terminal: stdin is the tar.
REMOTE_COMMAND = (
    'd=$(mktemp -d) && tar -xf - -C "$d" --no-same-owner && '
    "python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); "
    'from deadlift.bootstrap.bootstrap import main; sys.exit(main(sys.argv[1:]))\' "$d"; '
    's=$?; rm -rf "$d"; exit $s'
)

_SERVER = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:-]*")


def server_problem(server: str) -> str | None:
    """Why ``server`` can't be a Server's host name or address, or None when it can."""
    if _SERVER.fullmatch(server):
        return None
    return f"'{server}' isn't a host name or address"


def _sent_files(checkout: Path) -> list[tuple[Path, str]]:
    """Each file sent, with its name in the tar."""
    source = checkout / "src" / "deadlift"
    files = [(source / "__init__.py", "deadlift/__init__.py")]
    for package in SENT_PACKAGES:
        files += [
            (path, f"deadlift/{path.relative_to(source).as_posix()}")
            for path in sorted((source / package).rglob("*.py"))
            if "__pycache__" not in path.parts
        ]
    scripts = checkout / "server"
    if scripts.is_dir():
        files += [
            (path, f"server/{path.name}") for path in sorted(scripts.iterdir()) if path.is_file()
        ]
    return files


def _anyones(member: tarfile.TarInfo) -> tarfile.TarInfo:
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    return member


def payload(checkout: Path = CHECKOUT) -> bytes:
    """The tar sent to the Server."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path, name in _sent_files(checkout):
            tar.add(path, arcname=name, recursive=False, filter=_anyones)
    return buffer.getvalue()


def bootstrap_server(server: str) -> int:
    """Bootstraps ``server``, whose name has been checked, returning the exit code."""
    sys.stdout.write(f"Bootstrapping {server}\n")
    sys.stdout.flush()
    data = payload()
    # The maintainer's own ssh, with their agent, config and keys: their whole environment.
    env = scrubbed_env(keep=tuple(os.environ))
    ssh = start(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", f"root@{server}", REMOTE_COMMAND],
        env=env,
        stdin=Stream.CAPTURE,
        stdout=Stream.INHERIT,
        stderr=Stream.INHERIT,
    )
    stdin = ssh.stdin
    if stdin is not None:
        try:
            stdin.write(data)
            stdin.close()
        except BrokenPipeError:
            pass
    code = ssh.wait()
    if code == SSH_UNREACHABLE:
        reason(f"couldn't reach {server} over SSH as root, so nothing was changed.")
        return int(Exit.FAILED)
    return code


def main(argv: Sequence[str]) -> int:
    """``deadlift server bootstrap <server>``, with ``argv`` the arguments after the command."""
    why = "server bootstrap needs one <server>" if len(argv) != 1 else server_problem(argv[0])
    if why is not None:
        reason(why)
        sys.stderr.write(USAGE)
        sys.stderr.flush()
        return int(Exit.REFUSED)
    return bootstrap_server(argv[0])
