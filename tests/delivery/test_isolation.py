"""The delivery tests on a bootstrapped Server: what an App's containers can reach, and who owns
what they write (ADR 0001).

Only a Server that `deadlift server bootstrap` set up has the firewall and userns-remap, so these
run in CI on the runner the server-setup tests bootstrapped, and are skipped elsewhere. They
change the machine's network for their length, so they also need a throwaway machine.

DEADLIFT_TEST_BOOTSTRAPPED=true, as the CI job sets after the server-setup tests, makes them
required: they fail instead of being skipped when the machine isn't a bootstrapped Server.
"""

import contextlib
import json
import os
import random
import secrets
import socket
import socketserver
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from tests.support.harness import Harness, Server, docker
from tests.support.network import PROBE, external_ipv4_addresses, reach
from tests.support.throwaway import why_not_throwaway

OUTSIDE = Path(__file__).resolve().parents[1] / "support" / "outside.sh"


def _remapped() -> bool:
    try:
        return "name=userns" in docker("info", "--format", "{{json .SecurityOptions}}")
    except (RuntimeError, OSError):
        return False


WHY = why_not_throwaway() or (None if _remapped() else "Docker here doesn't run with userns-remap")
REQUIRED = os.environ.get("DEADLIFT_TEST_BOOTSTRAPPED") == "true"


@pytest.mark.skipif(not REQUIRED, reason="DEADLIFT_TEST_BOOTSTRAPPED isn't true")
def test_this_machine_is_a_bootstrapped_server_as_ci_says() -> None:
    assert WHY is None


@dataclass(frozen=True)
class Target:
    """Where something answers TCP, from the host's side or a container's."""

    host: str
    port: int


def reach_from_host(target: Target) -> str:
    """What connecting from the host gives: "reached", "timeout" (dropped) or the error code."""
    return reach(target.host, target.port)


class _AnswerTheHost(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        # The probes only connect, then drop the connection.
        with contextlib.suppress(OSError):
            cast(socket.socket, self.request).sendall(b"the host\n")


@dataclass(frozen=True)
class Bootstrapped:
    server: Server
    app: str
    host_port: int
    outside_port: int

    def web(self) -> str:
        containers = self.server.containers(self.app)
        web = next((name for name in containers.split("\n") if "-web-" in name), None)
        if web is None:
            raise AssertionError(f"no web container among {containers}")
        return web

    def reach_from_container(self, targets: list[Target]) -> list[str]:
        """Runs the probe in the App's routed container, as the App's own code could."""
        arg = json.dumps([{"host": target.host, "port": target.port} for target in targets])
        return cast(list[str], json.loads(docker("exec", self.web(), "python3", "-c", PROBE, arg)))


def by_host(targets: list[Target], results: list[str]) -> dict[str, str]:
    return {target.host: result for target, result in zip(targets, results, strict=False)}


@pytest.fixture(scope="class")
def bootstrapped(harness: Harness) -> Iterator[Bootstrapped]:
    server = harness.server()
    app = server.apps["a"].name
    outside_port = 18000 + random.randrange(1000)  # noqa: S311
    outside_name = secrets.token_hex(3)
    subprocess.run(
        ["sudo", "bash", str(OUTSIDE), "up", outside_name, str(outside_port)], check=True
    )
    listener = socketserver.ThreadingTCPServer(("0.0.0.0", 0), _AnswerTheHost)  # noqa: S104
    threading.Thread(target=listener.serve_forever, daemon=True).start()
    try:
        result = server.deliver(app, ["deploy", "feature-42", "--image", f"web={harness.image()}"])
        assert result.outcome == "done", result.output
        yield Bootstrapped(server, app, listener.server_address[1], outside_port)
    finally:
        listener.shutdown()
        listener.server_close()
        subprocess.run(["sudo", "bash", str(OUTSIDE), "down", outside_name], check=False)


@pytest.mark.skipif(WHY is not None, reason=WHY or "")
class TestAnAppsContainersOnABootstrappedServer:
    def test_cant_reach_any_of_the_hosts_own_addresses(self, bootstrapped: Bootstrapped) -> None:
        addresses = subprocess.run(
            ["ip", "-json", "-4", "address", "show"], capture_output=True, text=True, check=True
        ).stdout
        targets = [
            Target(host, bootstrapped.host_port) for host in external_ipv4_addresses(addresses)
        ]
        # Among them, a tailnet address of the host's own, as tailscale0 has on a Server.
        assert "100.80.214.1" in [target.host for target in targets]
        # The host's listener answers on every one of them...
        for target in targets:
            assert reach_from_host(target) == "reached", target.host

        # ...and a container's attempts are dropped.
        results = bootstrapped.reach_from_container(targets)

        assert by_host(targets, results) == {target.host: "timeout" for target in targets}

    def test_cant_reach_the_tailnet_169_254_169_254_or_private_ranges_beyond_the_host(
        self, bootstrapped: Bootstrapped
    ) -> None:
        port = bootstrapped.outside_port
        targets = [
            Target("100.80.213.2", port),
            Target("169.254.169.254", port),
            Target("10.213.0.2", port),
            Target("172.16.213.2", port),
            Target("192.168.213.2", port),
        ]
        # Each answers the host, so only the firewall stands between it and a container.
        for target in targets:
            assert reach_from_host(target) == "reached", target.host

        results = bootstrapped.reach_from_container(targets)

        assert by_host(targets, results) == {target.host: "timeout" for target in targets}

    def test_can_reach_the_internet(self, bootstrapped: Bootstrapped) -> None:
        assert bootstrapped.reach_from_container([Target("github.com", 443)]) == ["reached"]

    def test_a_file_root_makes_in_a_container_is_owned_by_an_unprivileged_user_on_the_host(
        self, bootstrapped: Bootstrapped
    ) -> None:
        web = bootstrapped.web()
        docker("exec", "--user", "root", web, "touch", "/data/made-by-root")
        assert docker("exec", web, "stat", "-c", "%u", "/data/made-by-root") == "0"

        volume = f"deadlift-{bootstrapped.app}-feature-42_data"
        mountpoint = docker("volume", "inspect", "--format", "{{.Mountpoint}}", volume)
        owner = subprocess.run(
            ["sudo", "stat", "-c", "%u", f"{mountpoint}/made-by-root"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        # Root in a container is the first of the user ids the Server gave Docker's remap user.
        subuid = subprocess.run(
            ["grep", "^dockremap:", "/etc/subuid"], capture_output=True, text=True, check=True
        ).stdout
        assert int(owner) != 0
        assert owner == subuid.split(":")[1]
