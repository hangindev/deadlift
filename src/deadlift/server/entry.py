"""deadlift-entry: a Server's one way in for an App's CI. Bootstrap installs it, owned by root, at
/usr/local/sbin/deadlift-entry, and `deadlift app add` gives each App's deploy user one sudo rule
that runs it and nothing else (ADR 0001):

  deadlift-<app> ALL=(root) NOPASSWD: /usr/local/sbin/deadlift-entry

Usage, as `deliver` calls it over Tailscale SSH:
  tar -cf - compose.yaml token |
    ssh deadlift-<app>@<server> sudo deadlift-entry --protocol <n> <operation> [arguments...]

It works out the App from the user who called it (SUDO_USER, deadlift-<app>), never from its
arguments, so a deploy user can only ever act on its own App. It reads the delivery, one tar on
stdin holding at most compose.yaml (the App's Compose template) and token (the job's registry
token) as plain files of at most 1 MiB between them unpacked, and refuses anything else. It
reads the tar itself, with Python's tarfile, rather than unpacking it with tar, so what it
checks is exactly what it keeps. It then runs the deploy script that bootstrap
installed beside it, which checks the operation's arguments and runs it under the Server's lock.
Nothing in a delivery is ever executed, and the delivery is removed afterwards, token included.

The delivery protocol is versioned, so a `deliver` newer than the Server is refused with a
reason saying to re-run bootstrap, rather than misread.

Exit codes: 2 when it refuses, changing and running nothing; otherwise the deploy script's
(0 done, 1 failed, 2 refused, 75 skipped).

Settings, for the tests only: DEADLIFT_STATE_DIR and DEADLIFT_LIB_DIR move the Server's state
and the installed deploy script. sudo's env_reset never passes them on, so on a Server they're
always the defaults.
"""

import lzma
import os
import re
import shutil
import tarfile
import tempfile
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from deadlift.core.outcome import Stop, reason
from deadlift.core.outcome import refuse as stop_refused
from deadlift.core.process import Stream, run, scrubbed_env

# The delivery protocols this entry command speaks. A new one is added here, by bootstrap, before
# any `deliver` sends it.
PROTOCOLS = ("1",)
# The most a delivery may hold: a Compose template and a token are far smaller.
MAX_DELIVERY_BYTES = 1024 * 1024
# What a delivery may hold.
DELIVERED = ("compose.yaml", "token")
# What the deploy script is given of this process's environment: what sudo's env_reset (and the
# tests' stand-in for it) leaves, and the tests' settings, which a Server never has.
DEPLOY_ENV = (
    "PATH",
    "HOME",
    "DOCKER_HOST",
    "DEADLIFT_STATE_DIR",
    "DEADLIFT_LIB_DIR",
    "DEADLIFT_DISK_USED",
)


def refuse(why: str) -> NoReturn:
    stop_refused(f"refused the delivery: {why}. Nothing was run.")


def app_of(user: str, state: Path) -> str:
    """The App whose deploy user ``user`` is, refusing anyone else."""
    app = user.removeprefix("deadlift-")
    if not (
        user.startswith("deadlift-")
        and re.fullmatch(r"[a-z0-9][a-z0-9-]*", app)
        and (state / "apps" / app / "app.conf").is_file()
    ):
        refuse(f"'{user or 'nobody'}' isn't the deploy user of an App on this Server")
    return app


def read_delivery(archive: Path) -> dict[str, bytes]:
    """The files of the delivery tar at ``archive``, by name: compose.yaml and token, each at
    most once, as plain files of at most :data:`MAX_DELIVERY_BYTES` unpacked between them.
    Refuses anything else. It's read here, member by member from each one's own header, and
    never unpacked by tar, so what's checked is exactly what's used, and a compressed archive
    stops being read once what it holds grows past the limit."""
    members: list[tarfile.TarInfo] = []
    unpacked = 0
    try:
        with tarfile.open(archive, "r:*") as tar:
            for member in tar:
                members.append(member)
                unpacked += member.size
                if unpacked > MAX_DELIVERY_BYTES:
                    refuse(f"it unpacks to over {MAX_DELIVERY_BYTES} bytes")
            # Only plain files: no directories, links or devices.
            if any(not member.isreg() for member in members):
                refuse("it holds something other than plain files")
            files: dict[str, bytes] = {}
            for member in members:
                name = member.name.removeprefix("./")
                if name not in DELIVERED:
                    refuse(
                        f"it holds '{member.name}', and only compose.yaml and token may be "
                        "delivered"
                    )
                if name in files:
                    refuse(f"it holds {name} twice")
                content = tar.extractfile(member)
                files[name] = b"" if content is None else content.read()
    except (tarfile.TarError, EOFError, OSError, zlib.error, lzma.LZMAError):
        refuse("it isn't a tar archive")
    return files


def _read_stdin(archive: Path) -> int:
    """``head -c <max + 1> >archive``: stdin, up to one byte over the limit. Returns its size."""
    size = 0
    with archive.open("wb") as file:
        while size <= MAX_DELIVERY_BYTES:
            chunk = os.read(0, min(65536, MAX_DELIVERY_BYTES + 1 - size))
            if not chunk:
                break
            file.write(chunk)
            size += len(chunk)
    return size


def _entry(argv: Sequence[str]) -> int:
    state = Path(os.environ.get("DEADLIFT_STATE_DIR") or "/srv/deadlift")
    lib = Path(os.environ.get("DEADLIFT_LIB_DIR") or "/usr/local/lib/deadlift")

    # The App, from the deploy user that sudo says called us.
    app = app_of(os.environ.get("SUDO_USER", ""), state)

    if argv[:1] != ["--protocol"] or len(argv) < 2 or not argv[1]:
        refuse("it named no protocol version. Use deadlift's deliver action, which sends one")
    protocol = argv[1]
    rest = list(argv[2:])
    if protocol not in PROTOCOLS:
        refuse(
            f"it uses delivery protocol '{protocol}', and this Server speaks only "
            f"{' '.join(PROTOCOLS)}. Re-run `deadlift server bootstrap` to bring the Server up "
            "to date"
        )

    operation = rest[0] if rest else ""
    if operation not in ("deploy", "rollback", "destroy", "sweep"):
        refuse(
            f"'{operation}' isn't an operation. The operations are deploy, rollback, destroy "
            "and sweep"
        )

    deliveries = state / "deliveries"
    deliveries.mkdir(mode=0o700, parents=True, exist_ok=True)
    delivery = Path(tempfile.mkdtemp(prefix="", dir=deliveries))
    try:
        return _deliver(app, delivery, lib, rest)
    finally:
        shutil.rmtree(delivery, ignore_errors=True)


def _deliver(app: str, delivery: Path, lib: Path, rest: Sequence[str]) -> int:
    archive = delivery / "archive.tar"
    files = delivery / "files"

    size = _read_stdin(archive)
    if size <= 0:
        refuse("stdin was empty, and a delivery is a tar archive")
    if size > MAX_DELIVERY_BYTES:
        refuse(f"it's over {MAX_DELIVERY_BYTES} bytes")
    delivered = read_delivery(archive)
    archive.unlink()
    files.mkdir()
    for name, content in delivered.items():
        (files / name).write_bytes(content)

    # Not exec, so the delivery is removed afterwards.
    done = run(
        [str(lib / "deploy"), "--app", app, "--delivery", str(files), *rest],
        env=scrubbed_env(keep=DEPLOY_ENV),
        stdout=Stream.INHERIT,
    )
    # A deploy script killed by a signal exits as a shell reports it.
    return done.code if done.code >= 0 else 128 - done.code


def main(argv: Sequence[str]) -> int:
    os.umask(0o077)
    try:
        return _entry(argv)
    except Stop as stop:
        if stop.why is not None:
            reason(stop.why)
        return int(stop.code)
