"""Running children: the one place deadlift starts another program (ADR 0004).

Every child gets an explicit environment, an :class:`Env`, never this process's own, so nothing
the caller set (a deploy user's variables, a Compose ``${VAR}`` it hopes to override) reaches
``docker`` or ``tailscale``. An :class:`Env` is only made by :func:`scrubbed_env`, so passing
``os.environ`` is a type error.

Every child also starts with no file descriptors but its three standard ones (``close_fds``,
and Python opens every other fd close-on-exec), so the Server's lock never outlives the command
holding it in a ``docker`` or ``tailscale`` process left running in the background. Its stdin is
/dev/null unless given, so it never reads what was meant for deadlift (such as a delivery), and
its stdout goes to our stderr unless captured, since stdout carries ``deadlift-output:`` lines.
"""

import os
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from types import MappingProxyType
from typing import IO, Any

SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
"""PATH for a child when this process has none to keep."""

# Exit codes the shell gives when a command can't be run, kept so callers can treat a missing
# program as Bash's `cmd || ...` did.
NOT_FOUND = 127
NOT_EXECUTABLE = 126


@dataclass(frozen=True)
class Env:
    """A child's whole environment. Make one with :func:`scrubbed_env`."""

    variables: Mapping[str, str]

    def __post_init__(self) -> None:
        for name, value in self.variables.items():
            if not name or "=" in name or "\0" in name or "\0" in value:
                raise ValueError(f"{name!r} can't be an environment variable")
        object.__setattr__(self, "variables", MappingProxyType(dict(self.variables)))

    def with_vars(self, variables: Mapping[str, str]) -> "Env":
        """This environment with ``variables`` added, or replacing those of the same name."""
        return Env({**self.variables, **variables})


def scrubbed_env(
    *,
    keep: Iterable[str] = ("PATH", "HOME"),
    set_vars: Mapping[str, str] | None = None,
    source: Mapping[str, str] | None = None,
) -> Env:
    """An environment holding only the variables named in ``keep`` that ``source`` (this
    process's environment by default) has, then ``set_vars``. PATH is :data:`SAFE_PATH` when
    neither gives one.

    On the Server, ``keep`` is the place to name the few variables sudo's ``env_reset`` (or
    systemd) leaves, such as PATH, which on a Server is sudo's ``secure_path`` and in the tests
    puts the fake ``tailscale`` first.
    """
    environ: Mapping[str, str] = os.environ if source is None else source
    variables = {name: environ[name] for name in keep if name in environ}
    variables.setdefault("PATH", SAFE_PATH)
    variables.update(set_vars or {})
    return Env(variables)


class Stream(Enum):
    """Where a child's standard stream goes, besides a file or fd of the caller's."""

    INHERIT = auto()
    """Shared with this process."""
    DEVNULL = auto()
    CAPTURE = auto()
    """Collected into :attr:`Completed.stdout` / :attr:`Completed.stderr` (with :func:`run`), or
    a pipe on the :class:`subprocess.Popen` (with :func:`start`)."""
    STDERR = auto()
    """For stdout: into this process's stderr."""


Target = Stream | int | IO[bytes] | IO[str]


@dataclass(frozen=True)
class Completed:
    """A child that has exited. ``stdout`` and ``stderr`` are what was captured, else empty."""

    argv: tuple[str, ...]
    code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0


def _target(target: Target, *, stdout: bool = False) -> Any:  # what subprocess takes
    match target:
        case Stream.INHERIT:
            return None
        case Stream.DEVNULL:
            return subprocess.DEVNULL
        case Stream.CAPTURE:
            return subprocess.PIPE
        case Stream.STDERR:
            # fd 2 itself: sys.stderr may be replaced by something with no fd.
            return None if not stdout else 2
        case _:
            return target


def _check_argv(argv: Sequence[str]) -> list[str]:
    if isinstance(argv, str):
        raise TypeError("argv is a sequence of arguments, not a command line for a shell")
    if not argv:
        raise ValueError("argv is empty")
    return list(argv)


def _decode(data: bytes | None) -> str:
    return "" if data is None else data.decode("utf-8", errors="replace")


def run(
    argv: Sequence[str],
    *,
    env: Env,
    cwd: str | Path | None = None,
    input: str | bytes | None = None,  # subprocess's own name for it
    stdin: Target = Stream.DEVNULL,
    stdout: Target = Stream.STDERR,
    stderr: Target = Stream.INHERIT,
    timeout: float | None = None,
) -> Completed:
    """Runs ``argv`` (no shell) with exactly ``env`` and waits for it.

    ``input``, when given, is written to its stdin (and ``stdin`` must be left alone). A program
    that can't be found or run gives code 127 or 126, as in a shell, with the reason in
    ``stderr`` when that's captured and on this process's stderr when not.
    """
    args = _check_argv(argv)
    if input is not None and stdin is not Stream.DEVNULL:
        raise ValueError("give a child input or stdin, not both")
    data = input.encode() if isinstance(input, str) else input
    try:
        done = subprocess.run(
            args,
            env=dict(env.variables),
            cwd=cwd,
            input=data,
            stdin=None if data is not None else _target(stdin),
            stdout=_target(stdout, stdout=True),
            stderr=_target(stderr),
            timeout=timeout,
            close_fds=True,
            check=False,
        )
    except (FileNotFoundError, PermissionError) as error:
        code = NOT_FOUND if isinstance(error, FileNotFoundError) else NOT_EXECUTABLE
        message = f"{args[0]}: {error.strerror}\n"
        if stderr is Stream.CAPTURE:
            return Completed(tuple(args), code, stderr=message)
        sys.stderr.write(message)
        sys.stderr.flush()
        return Completed(tuple(args), code)
    return Completed(tuple(args), done.returncode, _decode(done.stdout), _decode(done.stderr))


def start(
    argv: Sequence[str],
    *,
    env: Env,
    cwd: str | Path | None = None,
    stdin: Target = Stream.DEVNULL,
    stdout: Target = Stream.STDERR,
    stderr: Target = Stream.INHERIT,
    new_session: bool = False,
) -> "subprocess.Popen[bytes]":
    """Starts ``argv`` (no shell) with exactly ``env``, and returns at once: for pipelines, such
    as a tar streamed into ``ssh``, and children left running. :attr:`Stream.CAPTURE` gives a
    pipe. ``new_session`` detaches it from this process's session and terminal. The caller
    waits for it (or means not to). Raises FileNotFoundError when there's no such program.
    """
    return subprocess.Popen(
        _check_argv(argv),
        env=dict(env.variables),
        cwd=cwd,
        stdin=_target(stdin),
        stdout=_target(stdout, stdout=True),
        stderr=_target(stderr),
        close_fds=True,
        start_new_session=new_session,
    )
