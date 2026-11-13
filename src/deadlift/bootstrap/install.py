"""Installs the Server's code: ``deadlift.server`` and ``deadlift.core`` under the Server's lib
directory, and a launcher for each entry point (ADR 0004).

A launcher is a few lines of Python, ``#!/usr/bin/python3 -I``, with the directory deadlift's
modules load from written into it at install time: on a Server, /usr/local/lib/deadlift/python,
root's alone. It never works the directory out from its own path or the environment, so nothing a
deploy user can write is ever on ``sys.path``. The tests install the same launchers with their own
directories, through :func:`main`::

    python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); \\
      from deadlift.bootstrap.install import main; sys.exit(main(sys.argv[2:]))' \\
      <checkout>/src --lib <dir> --sbin <dir> --no-chown

Everything is installed as bootstrap installs files: compared first, so a run with nothing to do
changes nothing, and written aside then renamed, so a run cut off half way never leaves half a
file. Standard library only.
"""

import argparse
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SERVER_LIB = Path("/usr/local/lib/deadlift")
SERVER_SBIN = Path("/usr/local/sbin")
SYSTEM_PYTHON = "/usr/bin/python3"
# The packages a Server runs, relative to src/deadlift.
SERVER_PACKAGES = ("core", "server")


def python_dir(lib: Path) -> Path:
    """Where the Server's modules are: a directory holding only the ``deadlift`` package."""
    return lib / "python"


@dataclass(frozen=True)
class Launcher:
    """An entry point on the Server: ``<sbin or lib>/<name>`` runs ``<module>.main(argv)``."""

    where: Literal["sbin", "lib"]
    name: str
    module: str
    description: str


# Every entry point a Server has. A port of a Server script adds its line here (and bootstrap
# installs it), keeping the installed path the Bash script had, so whatever calls it (the entry
# command, sudo's rule, systemd, `deadlift app` over SSH) doesn't change.
LAUNCHERS: tuple[Launcher, ...] = ()


def launcher_source(module: str, *, modules: Path, description: str) -> str:
    """The launcher for ``module``, loading deadlift from ``modules`` and nowhere else."""
    if not modules.is_absolute():
        raise ValueError(f"a launcher's modules directory must be absolute, not {modules}")
    lines = [
        f"#!{SYSTEM_PYTHON} -I",
        f'"""{description}',
        "",
        "Installed by deadlift's bootstrap (ADR 0004). It runs in Python's isolated mode, -I: no",
        "PYTHON* variables, no user site-packages, and neither the working directory nor this",
        "file's directory on sys.path. deadlift's modules load only from the directory below,",
        'which is root\'s alone, and no bytecode is written there."""',
        "",
        "import sys",
        "",
        "if not sys.flags.isolated:",
        '    sys.stderr.write("deadlift: run this by its own path, or as python3 -I\\n")',
        "    sys.exit(2)",
        "sys.dont_write_bytecode = True",
        f"sys.path.insert(0, {str(modules)!r})",
        "",
        f"from {module} import main  # noqa: E402",
        "",
        "sys.exit(main(sys.argv[1:]))",
        "",
    ]
    return "\n".join(lines)


def install_bytes(content: bytes, target: Path, mode: int, *, chown: bool) -> bool:
    """Installs ``content`` at ``target`` with ``mode`` (owned by root with ``chown``), unless
    it's there already just so. Returns whether it wrote."""
    try:
        status = target.lstat()
    except FileNotFoundError:
        status = None
    if (
        status is not None
        and target.is_file()
        and not target.is_symlink()
        and (status.st_mode & 0o7777) == mode
        and (not chown or (status.st_uid, status.st_gid) == (0, 0))
        and target.read_bytes() == content
    ):
        return False
    aside = target.with_name(f"{target.name}.new")
    aside.unlink(missing_ok=True)
    fd = os.open(aside, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(content)
    if chown:
        os.chown(aside, 0, 0)
    aside.chmod(mode)
    aside.replace(target)
    return True


def _install_dir(path: Path, *, chown: bool) -> None:
    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    path.chmod(0o755)
    if chown:
        os.chown(path, 0, 0)


def install_package(source: Path, lib: Path, *, chown: bool) -> bool:
    """Installs the Server's packages from ``source`` (src/deadlift in a checkout) into
    :func:`python_dir`, removing any module there that ``source`` no longer has. Returns
    whether anything changed."""
    root = python_dir(lib)
    target = root / "deadlift"
    wanted = {Path("__init__.py")}
    for package in SERVER_PACKAGES:
        wanted.update(
            path.relative_to(source)
            for path in (source / package).rglob("*.py")
            if "__pycache__" not in path.parts
        )
    changed = False
    _install_dir(root, chown=chown)
    for relative in sorted(wanted):
        _install_dir(target / relative.parent, chown=chown)
        content = (source / relative).read_bytes()
        changed |= install_bytes(content, target / relative, 0o644, chown=chown)
    for path in sorted(target.rglob("*"), reverse=True):
        relative = path.relative_to(target)
        if path.is_dir() and not path.is_symlink():
            if not any(wanted_path.is_relative_to(relative) for wanted_path in wanted):
                shutil.rmtree(path)
                changed = True
        elif relative not in wanted:
            path.unlink()
            changed = True
    return changed


def install_launchers(launchers: Sequence[Launcher], *, lib: Path, sbin: Path, chown: bool) -> bool:
    """Installs each launcher, loading the modules :func:`install_package` put under ``lib``.
    Returns whether any changed."""
    changed = False
    for launcher in launchers:
        directory = sbin if launcher.where == "sbin" else lib
        _install_dir(directory, chown=chown)
        source = launcher_source(
            launcher.module, modules=python_dir(lib), description=launcher.description
        )
        changed |= install_bytes(source.encode(), directory / launcher.name, 0o755, chown=chown)
    return changed


def main(argv: Sequence[str]) -> int:
    """Installs the Server's code from this checkout into ``--lib`` and ``--sbin``, as bootstrap
    does: for the tests, which install it on a fake Server with ``--no-chown``."""
    parser = argparse.ArgumentParser(prog="deadlift.bootstrap.install")
    parser.add_argument("--lib", type=Path, default=SERVER_LIB)
    parser.add_argument("--sbin", type=Path, default=SERVER_SBIN)
    parser.add_argument("--no-chown", action="store_true", help="leave files owned by the caller")
    args = parser.parse_args(argv)
    lib: Path = args.lib.absolute()
    sbin: Path = args.sbin.absolute()
    chown = not args.no_chown
    source = Path(__file__).resolve().parent.parent
    install_package(source, lib, chown=chown)
    install_launchers(LAUNCHERS, lib=lib, sbin=sbin, chown=chown)
    return 0
