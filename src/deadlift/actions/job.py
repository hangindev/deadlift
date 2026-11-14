"""What deadlift's composite actions share: the CI job they run in.

The actions run as the job's own user on a GitHub runner, from ``$GITHUB_ACTION_PATH``, on the
runner's system python3. They aren't privileged, and whoever set the job's environment is the
App's own workflow, so their children (``gh``, ``docker``, ``ssh``, ``sudo``) get the job's
whole environment, as the Bash scripts' did: ``gh`` finds its token and host there, ``docker``
its builder and ``ssh`` its agent. It's still passed explicitly, through
:mod:`deadlift.core.process`.
"""

import json
import os
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import NoReturn

from deadlift.core.outcome import Exit, Stop, reason
from deadlift.core.process import Env, scrubbed_env

# A shell's default IFS: how Bash's `read -a` split a line into words.
_WORDS = re.compile(r"[ \t\n]+")


def job_env(set_vars: Mapping[str, str] | None = None) -> Env:
    """The job's whole environment, with ``set_vars`` added, for an action's children."""
    return scrubbed_env(keep=tuple(os.environ), set_vars=set_vars)


def words(text: str) -> list[str]:
    """``text`` split into words on spaces, tabs and newlines, as Bash's ``read -a`` does."""
    return [word for word in _WORDS.split(text) if word]


def usage(why: str, usage_line: str) -> NoReturn:
    """Refuses a bad command line: the reason, then the ``Usage:`` line, both on stderr."""
    reason(why)
    sys.stderr.write(f"Usage: {usage_line}\n")
    sys.stderr.flush()
    raise Stop(Exit.REFUSED, None)


def say(line: str) -> None:
    """Writes a line to stdout at once, so it stays in order with stderr in a merged log."""
    sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


def append(variable: str, lines: Iterable[str]) -> bool:
    """Appends ``lines`` to the file the job's ``variable`` names (``GITHUB_OUTPUT``,
    ``GITHUB_STEP_SUMMARY``), when it's set. Returns whether it was."""
    path = os.environ.get(variable, "")
    if not path:
        return False
    with Path(path).open("a", encoding="utf-8", errors="surrogateescape") as file:
        file.writelines(f"{line}\n" for line in lines)
    return True


def json_values(text: str) -> list[object]:
    """The JSON values in ``text``, one after another, as ``jq -s`` reads them (such as each
    line of ``gh api --jq``). ValueError when it isn't JSON."""
    decoder = json.JSONDecoder()
    values: list[object] = []
    at = 0
    while True:
        while at < len(text) and text[at] in " \t\r\n":
            at += 1
        if at == len(text):
            return values
        value, at = decoder.raw_decode(text, at)
        values.append(value)
