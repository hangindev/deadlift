"""The server-setup tests: an App's place on a Server. Runs the real `deadlift app add`, `app
secrets`, `app remove` and `server status` against this machine over real SSH as root, after the
real bootstrap, with only `tailscale` and the Tailscale API faked. A delivery then reaches the
App's Dev Environment over real SSH as its deploy user, through its real sudo rule. Like
test_bootstrap.py, it refuses to start anywhere but a throwaway machine.
"""

import json
import re
import shutil
import tempfile
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from tests.server_setup.maintainer import REPO, Maintainer, last_reason
from tests.support.commands import Result, ok, run
from tests.support.fake_tailscale_api import (
    FAKE_API_KEY,
    ApiRequest,
    FakeTailscaleApi,
    fake_tailscale_api,
)


@dataclass
class Apps:
    """Two Apps, named so nothing else on the machine could be theirs, and the maintainer who
    adds them to this machine through the fake Tailscale API."""

    maintainer: Maintainer
    api: FakeTailscaleApi
    remembered: dict[str, str] = field(default_factory=dict[str, str])
    """What one test keeps for a later one."""

    @property
    def a(self) -> str:
        return f"t{self.maintainer.hex}a"

    @property
    def b(self) -> str:
        return f"t{self.maintainer.hex}b"

    def env(self, **more: str) -> dict[str, str]:
        """The maintainer's environment: their SSH, and their Tailscale credential for the fake
        API."""
        return self.maintainer.env(
            TAILSCALE_API_KEY=FAKE_API_KEY, DEADLIFT_TAILSCALE_API=self.api.url, **more
        )

    def deadlift(self, *args: str) -> Result:
        """`deadlift <args>`, as the maintainer runs it, at this machine."""
        return run(REPO / "bin/deadlift", *args, env=self.env())

    def add_a(self) -> Result:
        return self.deadlift(
            *("app", "add", self.a, "--server", "localhost", "--repo", "owner/app-a"),
            *("--slots", "2", "--snapshots", "1", "--memory", "256m", "--cpus", "0.5"),
            *("--pids", "100"),
        )

    def status_of(self, app: str) -> list[str] | None:
        """What `deadlift server status` shows for one App: its summary line and its
        Environments."""
        result = self.deadlift("server", "status", "localhost")
        assert result.code == 0, result.output
        lines = result.stdout.split("\n")
        start = next((i for i, line in enumerate(lines) if line.startswith(f"{app}: ")), -1)
        if start < 0:
            return None
        end = next(
            (i for i, line in enumerate(lines) if i > start and not line.startswith("  ")), -1
        )
        return lines[start:] if end < 0 else lines[start:end]

    def port_block(self, app: str) -> tuple[int, int]:
        """The block of loopback ports `server status` shows for an App."""
        summary = (self.status_of(app) or [""])[0]
        match = re.search(r"ports (\d+)-(\d+)", summary)
        assert match is not None, summary
        return int(match[1]), int(match[2])

    def deliver_dev(self, app: str, template: str) -> Result:
        """Delivers ``template`` to an App's Dev Environment as its CI would, through `deliver`,
        over real SSH as the App's deploy user."""
        job = Path(tempfile.mkdtemp(prefix="job-", dir=self.maintainer.work))
        (job / "compose.yaml").write_text(template)
        # As the deliver action runs it.
        result = run(
            "python3",
            "-I",
            REPO / "actions/deliver/deliver.py",
            *("--server", "localhost", "--app", app, "deploy", "dev", "--image", "web=alpine:3.20"),
            cwd=job,
            env=self.env(DEADLIFT_REGISTRY_TOKEN="test-registry-token"),  # noqa: S106 (a stand-in)
        )
        shutil.rmtree(job, ignore_errors=True)
        return result

    def api_writes(self) -> list[ApiRequest]:
        """Every request that changed something through the Tailscale API."""
        return [request for request in self.api.requests if request.method != "GET"]


def printed_policy(stdout: str) -> object:
    """The tailnet policy entries `app add` printed, as JSON."""
    lines = stdout.split("\n")
    start = lines.index("{") if "{" in lines else -1
    assert start >= 0, stdout
    end = lines.index("}", start)
    return json.loads("\n".join(lines[start : end + 1]))


def template(version: int) -> str:
    """A template whose routed service idles, healthy, with a named volume and a secret in its
    environment."""
    return f"""services:
  web:
    image: ${{DEADLIFT_IMAGE_WEB}}
    command: ["sh", "-c", "trap 'exit 0' TERM; sleep 600 & wait"]
    environment:
      GREETING: ${{GREETING:-unset}}
      VERSION: "{version}"
    volumes:
      - data:/data
    healthcheck:
      test: ["CMD", "true"]
      interval: 1s
      retries: 3
volumes:
  data:
x-deadlift:
  route: web:8080
"""


@pytest.fixture(scope="module")
def apps(maintainer: Maintainer) -> Generator[Apps]:
    with fake_tailscale_api() as api:
        apps = Apps(maintainer, api)
        try:
            # A Server to add Apps to. Bootstrap changes nothing when test_bootstrap.py ran
            # first.
            bootstrap = apps.deadlift("server", "bootstrap", "localhost")
            assert bootstrap.code == 0, bootstrap.output
            ok("docker", "pull", "--quiet", "alpine:3.20")
            yield apps
        finally:
            for app in [apps.a, apps.b]:
                apps.deadlift("app", "remove", app, "--server", "localhost", "--yes")


class TestDeadliftAppAdd:
    def test_gives_an_app_its_deploy_user_its_one_sudo_rule_its_service_and_its_policy_entries(
        self, apps: Apps
    ) -> None:
        a = apps.a
        result = apps.add_a()

        assert result.code == 0, result.output
        apps.remembered["first_stdout"] = result.stdout
        # The deploy user, outside the docker group.
        groups = ok("id", "-nG", f"deadlift-{a}").split(" ")
        assert "docker" not in groups
        # Its one sudo rule, for the entry command and nothing else, and sudo's rules all still
        # parse.
        sudo = ok("sudo", "-l", "-U", f"deadlift-{a}")
        commands = sudo[sudo.index("may run the following commands") :].split("\n")[1:]
        assert [line.strip() for line in commands if line.strip()] == [
            "(root) NOPASSWD: /usr/local/sbin/deadlift-entry",
        ]
        assert run("sudo", "visudo", "-c").code == 0
        # Its Service, through the API with the maintainer's credential, tagged for deadlift
        # and listing Dev's 443 and each of its 2 Slots' ports.
        service = apps.api.services.get(f"svc:{a}")
        assert service is not None
        assert service.tags == ["tag:deadlift-svc"]
        assert sorted(service.ports) == ["tcp:10001", "tcp:10002", "tcp:443"]
        assert all(
            request.authorization == f"Bearer {FAKE_API_KEY}" for request in apps.api_writes()
        )
        # Only the App's own policy entries: its CI tag, that tag's grant to the Server's SSH
        # port, and its SSH rule as the deploy user. Nothing for the Service.
        assert printed_policy(result.stdout) == {
            "tagOwners": {f"tag:deadlift-ci-{a}": ["autogroup:admin"]},
            "grants": [
                {"src": [f"tag:deadlift-ci-{a}"], "dst": ["tag:deadlift-server"], "ip": ["tcp:22"]}
            ],
            "ssh": [
                {
                    "action": "accept",
                    "src": [f"tag:deadlift-ci-{a}"],
                    "dst": ["tag:deadlift-server"],
                    "users": [f"deadlift-{a}"],
                },
            ],
        }
        # The federated credential its CI joins with, and the variables deliver reads.
        assert "https://token.actions.githubusercontent.com" in result.stdout
        assert "repo:owner/app-a:*" in result.stdout
        assert re.search(f"Auth Keys: Write, with the tag tag:deadlift-ci-{a}", result.stdout)
        assert "gh variable set TS_OAUTH_CLIENT_ID --repo owner/app-a" in result.stdout
        assert "gh variable set TS_AUDIENCE --repo owner/app-a" in result.stdout

    def test_gives_a_second_app_a_block_of_ports_that_doesnt_overlap_the_firsts(
        self, apps: Apps
    ) -> None:
        result = apps.deadlift(
            "app", "add", apps.b, "--server", "localhost", "--repo", "owner/app-b"
        )

        assert result.code == 0, result.output
        a_from, a_to = apps.port_block(apps.a)
        b_from, b_to = apps.port_block(apps.b)
        assert a_to - a_from == 99
        assert b_to - b_from == 99
        assert b_to < a_from or b_from > a_to
        # The default 3 Slots.
        service = apps.api.services.get(f"svc:{apps.b}")
        assert service is not None
        assert sorted(service.ports) == ["tcp:10001", "tcp:10002", "tcp:10003", "tcp:443"]

    def test_changes_nothing_when_run_again(self, apps: Apps) -> None:
        writes = len(apps.api_writes())
        marker = apps.maintainer.work / "marker"
        marker.write_text("")

        result = apps.add_a()

        assert result.code == 0, result.output
        changed = ok(
            *("sudo", "find", "/etc", "/srv/deadlift", f"/home/deadlift-{apps.a}"),
            *("-xdev", "-newer", marker),
        )
        assert changed == ""
        assert len(apps.api_writes()) == writes
        assert result.stdout == apps.remembered.get("first_stdout", "")

    def test_refuses_an_app_name_that_could_clash_with_another_apps_environments(
        self, apps: Apps
    ) -> None:
        result = apps.deadlift(
            "app", "add", f"{apps.a}-feature", "--server", "localhost", "--repo", "owner/x"
        )

        assert result.code == 2
        assert re.search(r"isn't an App name", last_reason(result) or "")
        assert apps.status_of(f"{apps.a}-feature") is None


class TestAnAppsSettingsAndSecretsOnTheServer:
    def test_reach_its_environments_deploys(self, apps: Apps) -> None:
        a = apps.a
        work = apps.maintainer.work
        # The deploy user logs in with the test's key, as Tailscale SSH would let its CI.
        home = f"/home/deadlift-{a}"
        user = f"deadlift-{a}"
        ok("sudo", "install", "-d", "-m", "0700", "-o", user, "-g", user, f"{home}/.ssh")
        ok(
            *("sudo", "install", "-m", "0600", "-o", user, "-g", user),
            *(work / "key.pub", f"{home}/.ssh/authorized_keys"),
        )
        secrets = work / "dev.env"
        secrets.write_text("GREETING=hello from app secrets\n")

        set_ = apps.deadlift("app", "secrets", a, "dev", str(secrets), "--server", "localhost")
        assert set_.code == 0, set_.output
        assert "from the next deploy of a Dev Environment on" in set_.output
        feature_secrets = work / "feature.env"
        feature_secrets.write_text("GREETING=hello from feature secrets\n")
        set_feature = apps.deadlift(
            "app", "secrets", a, "feature", str(feature_secrets), "--server", "localhost"
        )
        assert set_feature.code == 0, set_feature.output
        assert "from the next deploy of a Feature Environment on" in set_feature.output
        first = apps.deliver_dev(a, template(1))
        assert first.code == 0, first.output
        second = apps.deliver_dev(a, template(2))
        assert second.code == 0, second.output

        container = f"deadlift-{a}-dev-web-1"
        assert ok("docker", "exec", container, "printenv", "GREETING") == "hello from app secrets"
        # The limits app add was given, on the App's container.
        limits = ok(
            *("docker", "inspect", "--format"),
            "{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}} {{.HostConfig.PidsLimit}}",
            container,
        )
        assert limits == f"{256 * 1024 * 1024} 500000000 100"
        # Two Dev deploys, and only the 1 Volume Snapshot app add said to keep.
        status = apps.status_of(a) or []
        assert status, "no status"
        assert re.search(r"Slots in use: 0 of 2, Volume Snapshots: 1$", status[0])
        dev = next((line for line in status if line.strip().startswith("dev ")), None)
        assert dev is not None, status
        assert re.search(rf"1 of 1 running.*https://{a}\.tailtest\.ts\.net/", dev)


class TestDeadliftAppRemove:
    def test_refuses_without_yes_changing_nothing(self, apps: Apps) -> None:
        result = apps.deadlift("app", "remove", apps.a, "--server", "localhost")

        assert result.code == 2
        assert re.search(r"--yes", last_reason(result) or "")
        assert apps.status_of(apps.a) is not None

    def test_leaves_nothing_of_the_app_and_the_other_app_as_it_was(self, apps: Apps) -> None:
        a = apps.a
        result = apps.deadlift("app", "remove", a, "--server", "localhost", "--yes")

        assert result.code == 0, result.output
        by_label = ["--filter", f"label=deadlift.app={a}", "--quiet"]
        assert ok("docker", "container", "ls", "--all", *by_label) == ""
        assert ok("docker", "volume", "ls", *by_label) == ""
        assert ok("docker", "network", "ls", "--quiet", "--filter", f"name=deadlift-{a}-") == ""
        assert run("id", f"deadlift-{a}").code != 0
        assert (
            ok("sudo", "sh", "-c", f"grep -rl deadlift-{a} /etc/sudoers /etc/sudoers.d || true")
            == ""
        )
        assert f"svc:{a}" not in apps.api.services
        serve: object = json.loads(ok("sudo", "tailscale", "serve", "status", "--json"))
        assert isinstance(serve, dict)
        services: object = cast(dict[str, object], serve).get("Services") or {}
        assert isinstance(services, dict)
        assert f"svc:{a}" not in cast(dict[str, object], services)
        # Its Environments and Volume Snapshots are gone with it, and the other App is still
        # there.
        assert apps.status_of(a) is None
        assert apps.status_of(apps.b) is not None
        assert f"svc:{apps.b}" in apps.api.services

    def test_is_done_changing_nothing_when_the_app_is_gone_already(self, apps: Apps) -> None:
        result = apps.deadlift("app", "remove", apps.a, "--server", "localhost", "--yes")

        assert result.code == 0, result.output
        assert re.search(r"nothing to remove|no Tailscale Service", last_reason(result) or "")
