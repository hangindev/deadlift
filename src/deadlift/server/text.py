"""The Server's small text conventions, as its Bash scripts read and wrote them, so a Server's
files mean the same to the Python: ``name=value`` files (app.conf, the Server-wide
settings), the last line of a command's output in a reason, sentences naming several things, and
values printed as ``jq -r`` printed them.
"""

import json
from collections.abc import Sequence
from pathlib import Path


def read_settings_text(text: str) -> list[tuple[str, str]]:
    """Each line of a ``name=value`` file, as Bash's ``while IFS='=' read -r key value`` read
    it: the name up to the first ``=``, the value the rest, less one trailing ``=`` when it's the
    value's only one. A last line with no line break is read too, as a hand-edited file may end
    without one (where Bash's loop skipped it).
    """
    pairs: list[tuple[str, str]] = []
    for line in text.removesuffix("\n").split("\n") if text else []:
        key, _, rest = line.partition("=")
        if rest.endswith("=") and "=" not in rest[:-1]:
            rest = rest[:-1]
        pairs.append((key, rest))
    return pairs


def read_settings(path: Path) -> list[tuple[str, str]]:
    """:func:`read_settings_text` of the file at ``path``."""
    return read_settings_text(path.read_bytes().decode("utf-8", errors="surrogateescape"))


def last_line(text: str) -> str:
    """The last line of ``text``, as ``$(tail -n 1 file)`` gives it."""
    return text.removesuffix("\n").rpartition("\n")[2]


def lines(text: str) -> list[str]:
    """The lines of a command's output, as Bash's ``mapfile -t`` reads them."""
    return text.removesuffix("\n").split("\n") if text else []


def sentence(names: Sequence[str]) -> str:
    """Joins ``names`` as in a sentence: "a", "a and b", "a, b and c"."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def json_text(value: object) -> str:
    """``value`` as ``jq -r`` prints it, or as jq's string interpolation does: a string as it
    is, anything else as compact JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
