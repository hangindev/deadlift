"""Running commands from a test, as the maintainer's shell would: their output collected, nothing
on their stdin."""

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

type Arg = str | Path


@dataclass(frozen=True)
class Result:
    code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        """stdout, then stderr."""
        return self.stdout + self.stderr


def run(
    command: Arg, *args: Arg, env: Mapping[str, str] | None = None, cwd: Path | None = None
) -> Result:
    """Runs a command, collecting its exit code, stdout and stderr."""
    done = subprocess.run(
        [str(command), *map(str, args)],
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    return Result(done.returncode, done.stdout, done.stderr)


def ok(command: Arg, *args: Arg) -> str:
    """Runs a command that must succeed, returning its stdout without surrounding whitespace."""
    result = run(command, *args)
    if result.code != 0:
        shown = " ".join(map(str, [command, *args]))
        raise AssertionError(f"`{shown}` exited {result.code}:\n{result.output}")
    return result.stdout.strip()
