"""Where deadlift is on a Server: its installed code and its state, at fixed paths only root can
write, and never from the environment (ADR 0004).

The entry command runs as root for a deploy user, so nothing that user can set may choose what
root runs or where it writes. A sudo rule with ``env_keep``, or a ``SETENV`` tag, would pass the
caller's variables on, so none is read: these paths are constants, and the launcher bootstrap
installs (deadlift.bootstrap.install) is the only thing that can change them. On a Server, it
leaves them as they are. The tests install launchers that :func:`use` a layout of their own, in a
directory of theirs, written into the launcher at install time like its modules directory.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Layout:
    """Where the Server's code and state are."""

    lib: Path = Path("/usr/local/lib/deadlift")
    """The installed deploy script, template check and the rest, beside their modules."""
    state: Path = Path("/srv/deadlift")
    """The Server's state: its lock, settings and the Apps' registry entries and Environments."""
    disk_used: Path | None = None
    """For the tests only: a file holding how full the disk is, as a whole percentage, in place
    of measuring it. A Server's layout has none."""


# The one this process runs with.
_chosen = [Layout()]


def layout() -> Layout:
    """The layout this process runs with: a Server's, unless its launcher said otherwise."""
    return _chosen[0]


def use(chosen: Layout) -> None:
    """Sets the layout, once, from a launcher installed for the tests, before its entry point
    runs."""
    _chosen[0] = chosen
