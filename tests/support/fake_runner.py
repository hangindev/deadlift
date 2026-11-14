"""A fake GitHub runner for testing deadlift's actions through their action.yml.

A job's checkout, and the runner's tools faked on PATH ahead of any real ones
(test/fakes/runner). Its `ssh` stands in for the Server, keeping what each delivery sent and
answering as the test says. Nothing reaches the network, the tailnet or this machine's Docker,
and nothing runs as root.
"""

import json
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.support.composite_action import REPO_ROOT, ActionRun, UsesStub, run_action

FAKES = REPO_ROOT / "test/fakes/runner"


@dataclass(frozen=True)
class SentDelivery:
    """What one delivery sent the Server."""

    destination: str
    """Who it logged in as, and where: `deadlift-<app>@<server>`."""
    command: list[str]
    """The command the Server ran, word by word."""
    files: dict[str, str]
    """The delivery tar's members and their contents."""


@dataclass(frozen=True)
class Login:
    registry: str
    username: str
    password: str


@dataclass(frozen=True)
class PushedImage:
    """One image tag as the registry got it."""

    ref: str
    login: Login
    """The login it was pushed with."""
    platform: str
    """The platform it was built for: one, or several separated by commas."""
    provenance: str
    """Whether provenance and SBOM attestations were pushed with it, as buildx's --provenance and
    --sbom (true unless set)."""
    sbom: str
    context: str
    dockerfile: str
    target: str
    labels: list[str]


class FakeRunner:
    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="deadlift-test-runner-"))
        self.state = self.dir / "state"
        self.workspace = self.dir / "workspace"
        """The job's checkout, where the action runs."""
        self.home = self.dir / "home"
        """The runner user's home directory."""
        for d in (self.state, self.workspace, self.home):
            d.mkdir()
        self.env = {
            "PATH": f"{FAKES}:/usr/bin:/bin",
            "HOME": str(self.home),
            "FAKE_RUNNER_STATE": str(self.state),
        }

    def server_answers(self, output: str, status: int = 0) -> None:
        """Makes the Server answer the next deliveries with this output and exit code."""
        (self.state / "server-reply").write_text(output)
        (self.state / "server-status").write_text(str(status))

    def deliveries(self) -> list[SentDelivery]:
        """Every delivery the Server got, in order."""
        ssh = self.state / "ssh"
        if not ssh.is_dir():
            return []
        sent: list[SentDelivery] = []
        for call in sorted(ssh.iterdir(), key=lambda p: int(p.name)):
            delivery = call / "delivery"
            command: list[str] = json.loads((call / "args").read_text())
            sent.append(
                SentDelivery(
                    destination=(call / "destination").read_text().strip(),
                    command=command,
                    files={f.name: f.read_text() for f in sorted(delivery.iterdir())},
                )
            )
        return sent

    def pushed(self) -> list[PushedImage]:
        """Every image tag pushed to a registry, in order."""
        try:
            lines = (self.state / "pushed.jsonl").read_text().split("\n")
        except FileNotFoundError:
            return []
        images: list[PushedImage] = []
        for line in filter(None, lines):
            image: dict[str, Any] = json.loads(line)
            image["login"] = Login(**image["login"])
            images.append(PushedImage(**image))
        return images

    def calls(self) -> list[str]:
        """Every call to the runner's faked tools, such as `sudo tailscale logout`, in order."""
        try:
            return list(filter(None, (self.state / "calls.log").read_text().split("\n")))
        except FileNotFoundError:
            return []

    def run_action(
        self,
        action: str,
        inputs: Mapping[str, str],
        *,
        github: Mapping[str, str] | None = None,
        uses: Mapping[str, UsesStub] | None = None,
    ) -> ActionRun:
        """Runs a composite action from the repo on this runner."""
        return run_action(
            action,
            inputs=inputs,
            github={"token": "fake-job-token", **(github or {})},
            env=self.env,
            uses=uses,
            cwd=self.workspace,
        )

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
