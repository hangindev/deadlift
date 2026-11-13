"""The Server's code as bootstrap installs it, and how its launchers load it (ADR 0004)."""

from pathlib import Path

import pytest

from deadlift.bootstrap.install import (
    Launcher,
    install_bytes,
    install_launchers,
    install_package,
    launcher_source,
    main,
    python_dir,
)
from deadlift.core.process import Stream, run, scrubbed_env

SOURCE = Path(__file__).resolve().parents[2] / "src" / "deadlift"

# Stands in for an entry point: says where deadlift's modules came from, and how it was run.
PROBE = """\
import os
import sys
from collections.abc import Sequence

import deadlift
import deadlift.core.outcome


def main(argv: Sequence[str]) -> int:
    print(deadlift.__file__)
    print(deadlift.core.outcome.__file__)
    print(sys.flags.isolated, sys.dont_write_bytecode, os.getcwd(), *argv)
    return 75
"""


def plant(directory: Path) -> None:
    """What someone else could write: a deadlift package and a module shadowing the stdlib's."""
    (directory / "deadlift" / "core").mkdir(parents=True)
    for module in ("deadlift/__init__.py", "deadlift/core/__init__.py", "sys.py", "os.py"):
        (directory / module).write_text("raise SystemExit('planted code ran')\n")


@pytest.fixture
def server(tmp_path: Path) -> tuple[Path, Path]:
    lib, sbin = tmp_path / "lib", tmp_path / "sbin"
    install_package(SOURCE, lib, chown=False)
    (python_dir(lib) / "deadlift/server/probe.py").write_text(PROBE)
    install_launchers(
        [Launcher("sbin", "deadlift-probe", "deadlift.server.probe", "A probe.")],
        lib=lib,
        sbin=sbin,
        chown=False,
    )
    return lib, sbin


def test_a_launcher_loads_deadlift_only_from_its_install_location(
    server: tuple[Path, Path], tmp_path: Path
) -> None:
    lib, sbin = server
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    plant(hostile)
    plant(sbin)
    env = scrubbed_env(set_vars={"PYTHONPATH": str(hostile), "PYTHONSTARTUP": str(hostile)})

    done = run(
        [str(sbin / "deadlift-probe"), "deploy", "dev"],
        env=env,
        cwd=hostile,
        stdout=Stream.CAPTURE,
        stderr=Stream.CAPTURE,
    )

    assert (done.code, done.stderr) == (75, "")
    package, outcome, flags = done.stdout.splitlines()
    modules = python_dir(lib) / "deadlift"
    assert package == str(modules / "__init__.py")
    assert outcome == str(modules / "core/outcome.py")
    assert flags == f"1 True {hostile} deploy dev"
    assert not list(python_dir(lib).rglob("__pycache__"))


def test_a_launcher_refuses_to_run_without_isolation(server: tuple[Path, Path]) -> None:
    _, sbin = server
    done = run(
        ["/usr/bin/python3", str(sbin / "deadlift-probe")],
        env=scrubbed_env(),
        stdout=Stream.CAPTURE,
        stderr=Stream.CAPTURE,
    )
    assert (done.code, done.stdout) == (2, "")
    assert done.stderr.startswith("deadlift: ")


def test_a_launcher_names_its_modules_directory_and_no_other() -> None:
    source = launcher_source("deadlift.server.entry", modules=Path("/x/python"), description="E.")
    assert source.startswith("#!/usr/bin/python3 -I\n")
    assert "sys.path.insert(0, '/x/python')" in source
    assert "__file__" not in source
    assert "environ" not in source
    with pytest.raises(ValueError, match="absolute"):
        launcher_source("m", modules=Path("python"), description="E.")


def test_the_server_gets_core_and_server_only(tmp_path: Path) -> None:
    install_package(SOURCE, tmp_path, chown=False)
    root = python_dir(tmp_path)
    assert [path.name for path in root.iterdir()] == ["deadlift"]
    packages = sorted(path.name for path in (root / "deadlift").iterdir() if path.is_dir())
    assert packages == ["core", "server"]
    assert (root / "deadlift/core/lock.py").read_bytes() == (SOURCE / "core/lock.py").read_bytes()
    assert ((root / "deadlift/core/lock.py").stat().st_mode & 0o777) == 0o644


def test_installing_again_changes_nothing_and_drops_what_is_gone(tmp_path: Path) -> None:
    assert install_package(SOURCE, tmp_path, chown=False)
    assert not install_package(SOURCE, tmp_path, chown=False)
    stale = python_dir(tmp_path) / "deadlift/server/old"
    stale.mkdir()
    (stale / "gone.py").write_text("")
    assert install_package(SOURCE, tmp_path, chown=False)
    assert not stale.exists()


def test_a_file_is_rewritten_only_when_it_differs(tmp_path: Path) -> None:
    target = tmp_path / "deploy.sh"
    assert install_bytes(b"one", target, 0o755, chown=False)
    assert not install_bytes(b"one", target, 0o755, chown=False)
    assert install_bytes(b"one", target, 0o700, chown=False)
    assert install_bytes(b"two", target, 0o700, chown=False)
    assert target.read_bytes() == b"two"
    assert not (tmp_path / "deploy.sh.new").exists()


def test_the_tests_install_the_server_as_bootstrap_would(tmp_path: Path) -> None:
    lib, sbin = tmp_path / "lib", tmp_path / "sbin"
    assert main(["--lib", str(lib), "--sbin", str(sbin), "--no-chown"]) == 0
    assert (python_dir(lib) / "deadlift/core/process.py").is_file()
