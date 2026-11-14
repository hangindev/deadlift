"""deadlift's override (ADR 0001): what every Environment's containers run with, whatever the
App's template says, as Docker reports it."""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast

import pytest

from tests.support.harness import Harness, Limits, Server, docker, example_template

type Container = dict[str, Any]
"""A container as `docker inspect` describes it."""


def inspect(app: str, service: str) -> Container:
    return cast(
        list[Container], json.loads(docker("inspect", f"deadlift-{app}-feature-1-{service}-1"))
    )[0]


@dataclass(frozen=True)
class Deployed:
    server: Server
    app: str
    web: Container
    migrate: Container


class TestDeadliftsOverride:
    # The App may use 256 MiB, half a CPU and 200 processes in each container. Its template asks
    # for more memory for web, and less for migrate.
    @pytest.fixture(scope="class")
    def deployed(self, harness: Harness) -> Iterator[Deployed]:
        server = harness.server(limits=Limits(memory="256m", cpus="0.5", pids=200))
        app = server.apps["a"].name
        template = (
            example_template()
            .replace(
                "\n  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n",
                "\n  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n"
                "    mem_limit: 4g\n    pids_limit: -1\n",
                1,
            )
            .replace(
                '    command: ["python3", "migrate.py"]\n',
                '    command: ["python3", "migrate.py"]\n    mem_limit: 128m\n',
                1,
            )
        )
        result = server.deliver(
            app, ["deploy", "feature-1", "--image", f"web={harness.image()}"], template=template
        )
        assert result.outcome == "done", result.output
        yield Deployed(server, app, inspect(app, "web"), inspect(app, "migrate"))
        harness.remove_servers()

    def test_publishes_only_the_routed_service_and_only_on_the_hosts_loopback(
        self, deployed: Deployed
    ) -> None:
        assert deployed.web["HostConfig"]["PortBindings"] == {
            "3000/tcp": [
                {"HostIp": "127.0.0.1", "HostPort": str(deployed.server.slot_port("a", 1))}
            ]
        }
        assert (deployed.migrate["HostConfig"]["PortBindings"] or {}) == {}

    def test_limits_every_container_to_the_apps_memory_cpus_and_processes(
        self, deployed: Deployed
    ) -> None:
        web, migrate = deployed.web["HostConfig"], deployed.migrate["HostConfig"]
        assert web["Memory"] == 256 * 1024 * 1024
        assert web["NanoCpus"] == 500_000_000
        assert web["PidsLimit"] == 200
        assert migrate["NanoCpus"] == 500_000_000
        assert migrate["PidsLimit"] == 200

    def test_keeps_a_templates_own_limit_where_its_lower_than_the_apps(
        self, deployed: Deployed
    ) -> None:
        assert deployed.migrate["HostConfig"]["Memory"] == 128 * 1024 * 1024

    def test_sets_no_new_privileges_on_every_container(self, deployed: Deployed) -> None:
        assert "no-new-privileges:true" in deployed.web["HostConfig"]["SecurityOpt"]
        assert "no-new-privileges:true" in deployed.migrate["HostConfig"]["SecurityOpt"]

    def test_caps_every_containers_logs(self, deployed: Deployed) -> None:
        for container in (deployed.web, deployed.migrate):
            assert container["HostConfig"]["LogConfig"] == {
                "Type": "json-file",
                "Config": {"max-size": "10m", "max-file": "3"},
            }

    def test_labels_every_container_and_volume_with_its_app_and_environment(
        self, deployed: Deployed
    ) -> None:
        expected = {"deadlift.app": deployed.app, "deadlift.environment": "feature-1"}
        for container in (deployed.web, deployed.migrate):
            labels = cast(dict[str, str], container["Config"]["Labels"])
            assert {name: labels.get(name) for name in expected} == expected
        volume = cast(
            list[Container],
            json.loads(docker("volume", "inspect", f"deadlift-{deployed.app}-feature-1_data")),
        )
        labels = cast(dict[str, str], volume[0]["Labels"])
        assert {name: labels.get(name) for name in expected} == expected


def test_an_app_with_no_limits_set_gets_deadlifts_defaults_1_gib_1_cpu_and_512_processes(
    harness: Harness,
) -> None:
    server = harness.server()
    app = server.apps["a"].name

    result = server.deliver(app, ["deploy", "feature-1", "--image", f"web={harness.image()}"])

    assert result.outcome == "done", result.output
    web = inspect(app, "web")["HostConfig"]
    assert web["Memory"] == 1024 * 1024 * 1024
    assert web["NanoCpus"] == 1_000_000_000
    assert web["PidsLimit"] == 512
    harness.remove_servers()
