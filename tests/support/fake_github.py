"""A fake GitHub for testing deadlift's actions: a `gh` on PATH (fake_gh.py) backed by a JSON
state file. Tests set up the state, run an action's script, then read the state back to see what
the action did to GitHub. No network."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from tests.support.composite_action import REPO_ROOT, ActionRun, parse_output_file, run_action

FAKE_GH = Path(__file__).resolve().with_name("fake_gh.py")


class Container(TypedDict):
    tags: list[str]


class Metadata(TypedDict):
    package_type: Literal["container"]
    container: Container


class PackageVersion(TypedDict):
    id: int
    name: str
    """The manifest's digest, as GHCR names a version."""
    created_at: str
    metadata: Metadata


class HeadRepo(TypedDict):
    full_name: str


class Head(TypedDict):
    sha: str
    repo: HeadRepo


class Label(TypedDict):
    name: str


class PullRequest(TypedDict):
    number: int
    state: Literal["open", "closed"]
    head: Head
    labels: list[Label]


class IssueComment(TypedDict):
    id: int
    issue: int
    """The issue or pull request it's on."""
    body: str


class CheckRunOutput(TypedDict, total=False):
    title: str
    summary: str


class CheckRun(TypedDict):
    name: str
    head_sha: str
    status: str
    conclusion: NotRequired[str]
    details_url: NotRequired[str]
    output: NotRequired[CheckRunOutput]


class Repository(TypedDict):
    ownerType: Literal["User", "Organization"]
    branches: dict[str, str]
    """Branch name to the commit it points at."""
    commits: dict[str, list[str]]
    """Commit to its parents, the first parent first."""
    pulls: NotRequired[list[PullRequest]]
    comments: NotRequired[list[IssueComment]]
    """Comments on its issues and pull requests."""
    checkRuns: NotRequired[list[CheckRun]]
    """Check runs created on its commits, in order."""


class Failure(TypedDict):
    """Requests that fail, matched by method and the start of the path."""

    method: str
    pathPrefix: str
    status: NotRequired[int]


class GitHubState(TypedDict, total=False):
    repos: dict[str, Repository]
    owners: dict[str, Literal["User", "Organization"]]
    """Owner login to its type, which decides whether its packages are under users/ or orgs/."""
    packages: dict[str, dict[str, list[PackageVersion]]]
    """Owner login to package name (as GHCR names it, e.g. `shop/web`) to its versions."""
    maxPerPage: int
    """The most items GitHub returns per page, 100 by default; set it lower to test paging."""
    failures: list[Failure]


@dataclass(frozen=True)
class Run:
    status: int | None
    stdout: str
    stderr: str
    outputs: dict[str, str]
    """What the script wrote to $GITHUB_OUTPUT."""


class FakeGitHub:
    def __init__(self, initial: GitHubState) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="deadlift-test-gh-"))
        self.state_path = self.dir / "state.json"
        self.state_path.write_text(json.dumps(initial, indent=2))
        gh = self.dir / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            f'exec {shlex.quote(sys.executable)} -I {shlex.quote(str(FAKE_GH))} "$@"\n'
        )
        gh.chmod(0o755)
        self.env = {
            "PATH": f"{self.dir}:{os.environ['PATH']}",
            "HOME": str(self.dir),
            "FAKE_GITHUB_STATE": str(self.state_path),
        }
        """The environment a job needs to reach this fake through `gh`."""

    def state(self) -> GitHubState:
        state: GitHubState = json.loads(self.state_path.read_text())
        return state

    def update(self, change: Callable[[GitHubState], object]) -> None:
        """Changes the state, as GitHub changes while a run waits."""
        state = self.state()
        change(state)
        self.state_path.write_text(json.dumps(state, indent=2))

    def run(self, script: str, args: Sequence[str], env: Mapping[str, str] | None = None) -> Run:
        """Runs a script from the repo, with this fake's `gh` first on PATH."""
        output_file = self.dir / "github-output"
        output_file.write_text("")
        # As the actions run their scripts: Python ones as `python3 -I`.
        command = ["python3", "-I"] if script.endswith(".py") else ["bash"]
        result = subprocess.run(
            [*command, str(REPO_ROOT / script), *args],
            env={
                **self.env,
                "GH_TOKEN": "fake-token",
                "GITHUB_OUTPUT": str(output_file),
                **(env or {}),
            },
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return Run(
            result.returncode if result.returncode >= 0 else None,
            result.stdout,
            result.stderr,
            parse_output_file(output_file.read_text()),
        )

    def run_action(
        self, action: str, inputs: Mapping[str, str], github: Mapping[str, str] | None = None
    ) -> ActionRun:
        """Runs a composite action from the repo, with this fake's `gh` first on PATH."""
        return run_action(
            action,
            inputs=inputs,
            github={"token": "fake-token", **(github or {})},
            env=self.env,
        )

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
