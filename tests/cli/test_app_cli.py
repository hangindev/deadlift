"""``deadlift app`` refusing on the maintainer's computer, before it reaches the Server or the
Tailscale API. What it does on a Server, the server-setup tests check."""

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.support.app_repository import DEADLIFT


@dataclass(frozen=True)
class Result:
    code: int
    reason: str
    reached_server: bool


@dataclass(frozen=True)
class Maintainer:
    """A maintainer's computer whose ssh records that it was called, and reaches nothing."""

    dir: Path

    @property
    def ssh_calls(self) -> Path:
        return self.dir / "ssh-calls"

    def deadlift(self, args: Sequence[str], extra: dict[str, str] | None = None) -> Result:
        self.ssh_calls.unlink(missing_ok=True)
        env = {
            "PATH": f"{self.dir}:{os.environ.get('PATH', '')}",
            "HOME": str(self.dir),
            # Nothing listens here, so any call to the Tailscale API fails.
            "DEADLIFT_TAILSCALE_API": "http://127.0.0.1:9",
            **(extra or {}),
        }
        result = subprocess.run(
            [str(DEADLIFT), *args], capture_output=True, text=True, env=env, check=False
        )
        reasons = [line for line in result.stderr.split("\n") if line.startswith("deadlift: ")]
        return Result(result.returncode, reasons[-1] if reasons else "", self.ssh_calls.exists())


@pytest.fixture(scope="module")
def maintainer() -> Iterator[Maintainer]:
    path = Path(tempfile.mkdtemp(prefix="deadlift-test-cli-"))
    ssh = path / "ssh"
    ssh.write_text(f'#!/bin/sh\necho "$*" >>{path / "ssh-calls"}\nexit 255\n')
    ssh.chmod(0o755)
    yield Maintainer(path)
    shutil.rmtree(path, ignore_errors=True)


KEY = {"TAILSCALE_API_KEY": "tskey-api-fake"}

# The file `app secrets` is given that isn't there: the maintainer's dir is only known at run
# time, so the case names it relative to it.
MISSING = "<dir>/missing.env"

REFUSALS = [
    (
        "an App name with capitals",
        ["app", "add", "MyApp", "--server", "s", "--repo", "o/r"],
        r"isn't an App name",
    ),
    (
        "an App name too long for its deploy user",
        ["app", "add", "a" * 24, "--server", "s", "--repo", "o/r"],
        r"isn't an App name",
    ),
    (
        "an App name that could clash with another's Feature Environments",
        ["app", "add", "shop-feature", "--server", "s", "--repo", "o/r"],
        r"isn't an App name",
    ),
    ("app add with no repo", ["app", "add", "shop", "--server", "s"], r"--repo"),
    ("app add with no Server", ["app", "add", "shop", "--repo", "o/r"], r"--server is required"),
    (
        "app secrets for an Environment kind that isn't dev or feature",
        ["app", "secrets", "shop", "prod", "/dev/null", "--server", "s"],
        r"dev or feature",
    ),
    (
        "app secrets with no file there",
        ["app", "secrets", "shop", "dev", MISSING, "--server", "s"],
        r"no file",
    ),
    ("app remove without --yes", ["app", "remove", "shop", "--server", "s"], r"--yes"),
]


@pytest.mark.parametrize(
    ("args", "why"), [pytest.param(args, why, id=name) for name, args, why in REFUSALS]
)
def test_refuses(maintainer: Maintainer, args: list[str], why: str) -> None:
    args = [str(maintainer.dir / "missing.env") if arg == MISSING else arg for arg in args]

    result = maintainer.deadlift(args, KEY)

    assert result.code == 2
    assert re.search(why, result.reason)
    assert not result.reached_server


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["app", "add", "shop", "--server", "s", "--repo", "o/r"], id="app add"),
        pytest.param(["app", "remove", "shop", "--server", "s", "--yes"], id="app remove"),
    ],
)
def test_refuses_without_the_maintainers_tailscale_credential(
    maintainer: Maintainer, args: list[str]
) -> None:
    result = maintainer.deadlift(args)

    assert result.code == 2
    assert re.search(r"TAILSCALE_API_KEY", result.reason)
    assert not result.reached_server


def test_says_when_the_server_cant_be_reached(maintainer: Maintainer) -> None:
    result = maintainer.deadlift(["app", "add", "shop", "--server", "s", "--repo", "o/r"], KEY)

    assert result.code == 1
    assert re.search(r"couldn't reach s over SSH as root, so nothing was changed", result.reason)
