"""How every deadlift command ends: its exit code, its reason lines and its outputs.

Every reason line starts with ``deadlift: `` and goes to stderr; the last one says what happened.
Outputs for ``deliver`` (and whatever reads a Server's answer) are lines of their own on stdout,
``deadlift-output: <name>=<value>``. Exit codes: 0 done, 1 failed, 2 refused or bad usage
(nothing changed), 75 skipped.

A command stops early by raising :class:`Stop`, through :func:`refuse`, :func:`fail` or
:func:`skip`, and its ``main`` hands its body to :func:`run_main`, which prints the reason and
turns it into the exit code::

    def main(argv: Sequence[str]) -> int:
        return run_main(lambda: deploy(argv))
"""

import re
import sys
from collections.abc import Callable, Iterable
from enum import IntEnum
from typing import NoReturn, TextIO

REASON_PREFIX = "deadlift: "
OUTPUT_PREFIX = "deadlift-output: "

_OUTPUT_NAME = re.compile(r"[A-Za-z0-9_-]+")


class Exit(IntEnum):
    """deadlift's exit codes, the same for every command, on the Server, CI and the CLI."""

    DONE = 0
    FAILED = 1
    REFUSED = 2
    """Refused, or bad usage: nothing was changed."""
    SKIPPED = 75
    """Nothing was done, and that's not a failure (such as no free Slot); EX_TEMPFAIL."""


def reason(text: str, *, to: TextIO | None = None) -> None:
    """Writes the reason line ``deadlift: <text>`` to stderr (or ``to``), at once."""
    stream = sys.stderr if to is None else to
    stream.write(f"{REASON_PREFIX}{text}\n")
    stream.flush()


def say(text: str = "", *, end: str = "\n") -> None:
    """Writes ``text``, a line of progress, to stdout at once, so it stays in order with stderr
    and with what children write."""
    sys.stdout.write(text + end)
    sys.stdout.flush()


def tell(text: str) -> None:
    """Writes the line ``text`` to stderr at once, after anything already said on stdout: a
    warning, or a reason line written out whole."""
    sys.stdout.flush()
    sys.stderr.write(f"{text}\n")
    sys.stderr.flush()


def output(name: str, value: str, *, to: TextIO | None = None) -> None:
    """Writes the output line ``deadlift-output: <name>=<value>`` to stdout (or ``to``), at once.

    An output is one line, so a value with a line break in it is a bug: ValueError.
    """
    if not _OUTPUT_NAME.fullmatch(name):
        raise ValueError(f"{name!r} isn't an output name: one or more of A-Z, a-z, 0-9, _ and -")
    if "\n" in value or "\r" in value:
        raise ValueError(f"the output {name} would span lines: {value!r}")
    stream = sys.stdout if to is None else to
    stream.write(f"{OUTPUT_PREFIX}{name}={value}\n")
    stream.flush()


def parse_outputs(lines: Iterable[str]) -> dict[str, str]:
    """The outputs among ``lines`` (such as a Server's answer), by name; a later one wins."""
    outputs: dict[str, str] = {}
    for raw in lines:
        line = raw.rstrip("\r\n")
        if line.startswith(OUTPUT_PREFIX):
            name, sep, value = line.removeprefix(OUTPUT_PREFIX).partition("=")
            if sep and _OUTPUT_NAME.fullmatch(name):
                outputs[name] = value
    return outputs


class Stop(Exception):
    """Ends the command with ``code``, after the reason line for ``why`` (none when ``why`` is
    None, for when the reason has been given already, such as by a child)."""

    def __init__(self, code: Exit, why: str | None) -> None:
        super().__init__(why)
        self.code = code
        self.why = why


def refuse(why: str) -> NoReturn:
    """Stops with :attr:`Exit.REFUSED`: nothing was changed."""
    raise Stop(Exit.REFUSED, why)


def fail(why: str) -> NoReturn:
    """Stops with :attr:`Exit.FAILED`."""
    raise Stop(Exit.FAILED, why)


def skip(why: str) -> NoReturn:
    """Stops with :attr:`Exit.SKIPPED`."""
    raise Stop(Exit.SKIPPED, why)


def refuse_usage(why: str, usage: str) -> NoReturn:
    """Stops for bad usage: the reason line for ``why``, then ``usage`` (its ``Usage:`` lines),
    on stderr. Nothing was changed."""
    reason(why)
    sys.stderr.write(usage if usage.endswith("\n") else f"{usage}\n")
    sys.stderr.flush()
    raise Stop(Exit.REFUSED, None)


def run_main(body: Callable[[], Exit | int | None]) -> int:
    """Runs a command's body and returns its exit code: what it returns (DONE for None, or a
    child's exit code it ends with, such as the Server's through ssh), or the code of the
    :class:`Stop` it raised, after printing that Stop's reason line.

    A command with something to do before every reason line, as the deploy script prunes images,
    catches Stop itself instead.
    """
    try:
        result = body()
    except Stop as stop:
        if stop.why is not None:
            reason(stop.why)
        return int(stop.code)
    return int(Exit.DONE if result is None else result)
