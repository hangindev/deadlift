"""An App's repository on the maintainer's computer: a fresh git repository, where
``deadlift init`` runs as the maintainer would run it."""

import shutil
import subprocess
import tempfile
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DEADLIFT = Path(__file__).resolve().parents[2] / "bin/deadlift"


@dataclass(frozen=True)
class Run:
    status: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class AppRepository:
    dir: Path

    def init(self, args: Sequence[str], cwd: Path | None = None) -> Run:
        """Runs ``deadlift init`` with these arguments, in the repository or ``cwd``."""
        result = subprocess.run(
            [str(DEADLIFT), "init", *args],
            cwd=cwd or self.dir,
            capture_output=True,
            text=True,
            check=False,
        )
        return Run(result.returncode, result.stdout, result.stderr)

    def workflow(self, name: str) -> Path:
        """A workflow's path, by name without ``.yml``."""
        return self.dir / ".github/workflows" / f"{name}.yml"

    def read(self, name: str) -> str:
        return self.workflow(name).read_text()


@contextmanager
def app_repository_at() -> Generator[AppRepository]:
    """A fresh App repository in a temporary directory, removed afterwards."""
    path = Path(tempfile.mkdtemp(prefix="deadlift-test-app-"))
    try:
        subprocess.run(["git", "init", "--quiet", str(path)], check=False)
        yield AppRepository(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)
