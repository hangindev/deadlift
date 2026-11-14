"""The server-setup tests: setting up a Server. Runs the real `deadlift server bootstrap` against
this machine over real SSH as root, with only `tailscale` faked, so it reconfigures this
machine's Docker and firewall for good. It refuses to start anywhere but a throwaway machine: the
GitHub-hosted ubuntu-24.04 runner in CI, or a Multipass VM (tests/server_setup/multipass.sh).

In CI, the delivery tests then run on the Server this leaves behind (tests/delivery).
"""

import re
from collections.abc import Generator
from pathlib import Path

import pytest

from tests.server_setup.maintainer import REPO, Maintainer
from tests.support.commands import Result, ok, run


@pytest.fixture(scope="module")
def created() -> Generator[list[str]]:
    """The containers a test made, removed after the file's tests."""
    names: list[str] = []
    yield names
    for name in names:
        run("docker", "rm", "--force", name)


def bootstrap(maintainer: Maintainer) -> Result:
    """`deadlift server bootstrap`, as a maintainer runs it, at this machine."""
    return maintainer.deadlift("server", "bootstrap", "localhost")


def firewall() -> str:
    """This machine's firewall: every table's rules, without packet counters."""
    rules = "\n".join([ok("sudo", "iptables-save"), ok("sudo", "ip6tables-save")])
    return "\n".join(
        re.sub(r"\[\d+:\d+\]", "", line, count=1)
        for line in rules.split("\n")
        if not line.startswith("#")
    )


class TestDeadliftServerBootstrap:
    def test_turns_this_machine_into_a_server(self, maintainer: Maintainer) -> None:
        result = bootstrap(maintainer)

        assert result.code == 0, result.output
        reasons = [line for line in result.output.split("\n") if line.startswith("deadlift: ")]
        assert reasons, result.output
        assert re.search(r"is a Server", reasons[-1])
        # The daemon really remaps users, so root in a container is unprivileged on the host.
        assert re.search(
            r"name=userns", ok("docker", "info", "--format", "{{json .SecurityOptions}}")
        )
        # What runs as root is what bootstrap installed, owned by root and writable by no one
        # else. Each is a launcher for deadlift's Python, installed under lib/deadlift/python
        # (ADR 0004).
        for installed, source in [
            ("/usr/local/sbin/deadlift-entry", "server/entry.py"),
            ("/usr/local/lib/deadlift/deploy", "server/deploy.py"),
            ("/usr/local/lib/deadlift/template", "server/template.py"),
            ("/usr/local/lib/deadlift/app", "server/app.py"),
            ("/usr/local/lib/deadlift/firewall", "server/firewall.py"),
        ]:
            assert ok("stat", "--format", "%U:%G %a", installed) == "root:root 755"
            assert Path(installed).read_text().split("\n")[0] == "#!/usr/bin/python3 -I"
            module = Path("/usr/local/lib/deadlift/python/deadlift", source)
            assert ok("stat", "--format", "%U:%G %a", module) == "root:root 644"
            assert module.read_text() == (REPO / "src/deadlift" / source).read_text()
        # The Server-wide settings, with the disk thresholds' defaults.
        settings = ok("sudo", "cat", "/srv/deadlift/settings")
        assert re.search(r"^feature_deploy_disk_limit=80$", settings, re.MULTILINE)
        assert re.search(r"^watchdog_disk_limit=90$", settings, re.MULTILINE)

    def test_runs_the_watchdog_on_a_timer(self) -> None:
        assert ok("systemctl", "is-enabled", "deadlift-watchdog.timer") == "enabled"
        assert ok("systemctl", "is-active", "deadlift-watchdog.timer") == "active"

        # What the timer starts, run now: it measures this machine's disk, and finds no Feature
        # Environment to stop.
        since = ok("date", "+%Y-%m-%d %H:%M:%S")
        result = run("sudo", "systemctl", "start", "deadlift-watchdog.service")

        assert result.code == 0, result.output
        log = ok(
            "sudo",
            "journalctl",
            "--unit",
            "deadlift-watchdog.service",
            "--since",
            since,
            "--output",
            "cat",
        )
        assert re.search(r"^deadlift: the disk is \d+% full", log, re.MULTILINE)

    def test_changes_no_file_and_restarts_nothing_when_run_again(
        self, maintainer: Maintainer, created: list[str]
    ) -> None:
        # An Environment's container, running across the second run.
        container = f"{maintainer.prefix}-running"
        created.append(container)
        ok("docker", "run", "--detach", "--name", container, "alpine:3.20", "sleep", "600")

        def started_at() -> str:
            return ok("docker", "inspect", "--format", "{{.State.StartedAt}}", container)

        def docker_pid() -> str:
            return ok("systemctl", "show", "docker.service", "--property=MainPID", "--value")

        before = {"started": started_at(), "docker": docker_pid(), "rules": firewall()}
        marker = maintainer.work / "marker"
        marker.write_text("")

        result = bootstrap(maintainer)

        assert result.code == 0, result.output
        # Everywhere bootstrap installs: system config, root's commands, and the Server's state.
        changed = ok(
            "sudo",
            "find",
            "/etc",
            "/usr/local/sbin",
            "/usr/local/bin",
            "/usr/local/lib/deadlift",
            "/srv/deadlift",
            "-xdev",
            "-newer",
            marker,
        )
        assert changed == ""
        assert docker_pid() == before["docker"]
        assert started_at() == before["started"]
        assert ok("docker", "inspect", "--format", "{{.State.Running}}", container) == "true"
        assert firewall() == before["rules"]

    def test_keeps_a_server_wide_setting_the_maintainer_changed(
        self, maintainer: Maintainer
    ) -> None:
        ok(
            "sudo",
            "sed",
            "-i",
            "s/^feature_deploy_disk_limit=.*/feature_deploy_disk_limit=70/",
            "/srv/deadlift/settings",
        )

        result = bootstrap(maintainer)

        assert result.code == 0, result.output
        assert re.search(
            r"^feature_deploy_disk_limit=70$",
            ok("sudo", "cat", "/srv/deadlift/settings"),
            re.MULTILINE,
        )
        ok(
            "sudo",
            "sed",
            "-i",
            "s/^feature_deploy_disk_limit=.*/feature_deploy_disk_limit=80/",
            "/srv/deadlift/settings",
        )

    def test_removes_the_launchers_an_earlier_bootstrap_installed_with_a_sh_suffix(
        self, maintainer: Maintainer
    ) -> None:
        lib = Path("/usr/local/lib/deadlift")
        old = [lib / name for name in ("deploy.sh", "template.sh", "app.sh", "firewall.sh")]
        # As an earlier bootstrap left them, and a file that's not deadlift's.
        for path in old:
            ok("sudo", "cp", "--preserve=mode", lib / path.stem, path)
        notes = lib / "notes.sh"
        ok("sudo", "touch", notes)
        try:
            result = bootstrap(maintainer)

            assert result.code == 0, result.output
            for path in old:
                assert not path.exists()
                assert f"Removed {path}" in result.output
            assert notes.exists()
        finally:
            ok("sudo", "rm", "--force", notes, *old)
