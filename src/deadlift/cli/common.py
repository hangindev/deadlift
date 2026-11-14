"""What the maintainer's commands share: how they end, the check on a Server's name,
and the environment their children run with.

The CLI runs on the maintainer's own computer (macOS or Linux), as the maintainer, so its children
(``ssh``, ``git``, ``tar``, ``tailscale``, a browser opener) get the maintainer's whole
environment, as they did from Bash: ``ssh`` needs SSH_AUTH_SOCK and the like, and there's no one
else's environment to keep out. They still run through ``deadlift.core.process``, which never uses
a shell.
"""

import io
import os
import re
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from deadlift.core.outcome import Exit, Stop, reason
from deadlift.core.process import Env, scrubbed_env

# The checkout bin/deadlift runs from: src/deadlift/cli/common.py is four levels below it.
ROOT = Path(__file__).resolve().parents[3]

_SERVER = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:-]*")


# `deadlift`'s usage, printed after the reason line of bad usage: the same lines Bash printed
# from bin/deadlift's header.
USAGE = (
    "Usage: deadlift server provision\n"
    "Usage: deadlift server bootstrap <server>\n"
    "Usage: deadlift server status <server>\n"
    "Usage: deadlift app add <app> --server <server> --repo <owner/name> [--slots <n>]"
    " [--snapshots <n>] [--memory <size>] [--cpus <n>] [--pids <n>]\n"
    "Usage: deadlift app secrets <app> dev|feature <file> --server <server>\n"
    "Usage: deadlift app remove <app> --server <server> --yes\n"
    "Usage: deadlift init <app> --server <server> [--branch <branch>] [--image <name>]...\n"
)


class Usage(Exception):
    """Bad usage: the reason line ``deadlift: <why>``, then ``usage``'s lines, and exit 2."""

    def __init__(self, why: str, usage: str) -> None:
        super().__init__(why)
        self.why = why
        self.usage = usage


class Exited(Exception):
    """Ends the command with ``code`` and no reason line of its own, as a Bash script ended with
    the status of a child that had said why already (such as the Server's app)."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def usage(why: str) -> NoReturn:
    """Stops for bad usage of ``deadlift``: nothing was changed."""
    raise Usage(why, USAGE)


def is_server_name(server: str) -> bool:
    """Whether ``server`` is a host name or address ``ssh root@`` may take."""
    return _SERVER.fullmatch(server) is not None


def maintainer_env() -> Env:
    """The maintainer's own environment, for a child of the CLI (see the module's docstring)."""
    return scrubbed_env(keep=tuple(os.environ))


def _bytes_as_given(stream: object) -> None:
    # What deadlift prints is UTF-8, whatever the locale says, and an argument that isn't (such as
    # a file name) goes back out as the bytes it came in as, as Bash printed it.
    if isinstance(stream, io.TextIOWrapper):
        stream.reconfigure(encoding="utf-8", errors="surrogateescape")


def run_command(body: Callable[[], Exit | int | None]) -> int:
    """Runs a command's body and returns its exit code, printing the reason line (and the usage,
    for bad usage) of how it stopped. Dies of SIGINT on Ctrl-C and of SIGPIPE on a closed pipe,
    as a shell script does."""
    _bytes_as_given(sys.stdout)
    _bytes_as_given(sys.stderr)
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    try:
        result = body()
    except Usage as usage:
        reason(usage.why)
        sys.stderr.write(usage.usage)
        sys.stderr.flush()
        return int(Exit.REFUSED)
    except Stop as stop:
        if stop.why is not None:
            reason(stop.why)
        return int(stop.code)
    except Exited as exited:
        return exited.code
    except KeyboardInterrupt:
        die_of_sigint()
    return int(Exit.DONE if result is None else result)


def die_of_sigint() -> NoReturn:
    """Ends this process as SIGINT ends a shell script, so whatever ran it sees the signal."""
    sys.stdout.flush()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGINT)
    # Not reached, unless SIGINT is blocked.
    raise SystemExit(130)
