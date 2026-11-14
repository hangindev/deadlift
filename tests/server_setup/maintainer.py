"""The maintainer, as the server-setup tests play them: their computer is this machine, which
they reach over real SSH as root, as they would a Server."""

import os
from dataclasses import dataclass
from pathlib import Path

from tests.support.commands import Result, run

REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Maintainer:
    """The maintainer's computer: a work directory, and an SSH that root on this machine
    accepts."""

    work: Path
    hex: str
    """This run's random part, in every name it makes."""

    @property
    def prefix(self) -> str:
        return f"deadlift-test-{self.hex}"

    def env(self, **more: str) -> dict[str, str]:
        """The maintainer's environment: their SSH first on PATH, and ``more``."""
        return {**os.environ, "PATH": f"{self.work / 'bin'}:{os.environ['PATH']}", **more}

    def deadlift(self, *args: str) -> Result:
        """`deadlift <args>`, as the maintainer runs it, at this machine."""
        return run(REPO / "bin/deadlift", *args, env=self.env())


def last_reason(result: Result) -> str | None:
    """The last reason line a command printed."""
    reasons = [line for line in result.output.split("\n") if line.startswith("deadlift: ")]
    return reasons[-1] if reasons else None
