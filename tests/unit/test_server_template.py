"""The template check's pure parts: the allowlist over a normalised Compose model, the keys that
would make Compose read a file on the Server, and deadlift's override (ADR 0001)."""

import json
from pathlib import Path
from typing import Any

import pytest

from deadlift.server.template import (
    Limits,
    Unchecked,
    allowlist_problem,
    file_reading_key,
    override_document,
    parse_limits,
)

PROJECT = "deadlift-shop-dev"


def model(services: dict[str, Any], **top: Any) -> dict[str, Any]:
    return {"name": PROJECT, "services": services, **top}


def web(**keys: Any) -> dict[str, Any]:
    return {"image": "example/web:1", "command": None, "entrypoint": None, **keys}


def test_a_plain_template_has_no_problem() -> None:
    config = model(
        {
            "web": web(
                networks={"default": None},
                volumes=[{"type": "volume", "source": "data", "target": "/data", "volume": {}}],
            )
        },
        volumes={"data": {"name": f"{PROJECT}_data"}},
        networks={"default": {"name": f"{PROJECT}_default", "ipam": {}}},
        **{"x-deadlift": {"route": "web:8080"}},
    )
    assert allowlist_problem(config) is None


@pytest.mark.parametrize(
    ("config", "problem"),
    [
        (
            model({"web": web(privileged=True)}),
            "services.web.privileged is true: it gives the container the whole Server",
        ),
        (model({"web": web(privileged=False)}), None),
        (
            model({"web": web(network_mode="host")}),
            "services.web.network_mode is host: only the project's own networks are allowed, "
            "never the host's network or a shared one",
        ),
        (model({"web": web(network_mode="service:db")}), None),
        (
            model({"web": web(pid="container:x")}),
            "services.web.pid is container:x: another container's namespace (container:) "
            "reaches outside the App's project",
        ),
        (
            model({"web": web(ipc="host")}),
            "services.web.ipc is host: it shares the Server's IPC namespace",
        ),
        (
            model(
                {
                    "web": web(
                        volumes=[{"type": "bind", "source": "/var/run/docker.sock", "target": "/s"}]
                    )
                }
            ),
            "services.web.volumes mounts the Docker socket, /var/run/docker.sock, which reaches "
            "every App on the Server",
        ),
        (
            model({"web": web(volumes=[{"type": "bind", "source": "/etc", "target": "/e"}])}),
            "services.web.volumes bind-mounts /etc from the Server: only named volumes and tmpfs "
            "are allowed",
        ),
        (
            model(
                {
                    "web": web(
                        volumes=[
                            {
                                "type": "volume",
                                "source": "d",
                                "target": "/d",
                                "volume": {},
                                "bind": {},
                                "a": 1,
                            }
                        ]
                    )
                }
            ),
            "services.web.volumes sets a on a mount: it isn't on deadlift's allowlist",
        ),
        (
            model({"web": web(volumes=[{"type": "npipe", "source": "x", "target": "/d"}])}),
            "services.web.volumes mounts a npipe: only named volumes and tmpfs are allowed",
        ),
        (
            model({"web": web(networks={"default": {"ipv4_address": "10.0.0.2"}})}),
            "services.web.networks sets ipv4_address on a network: it isn't on deadlift's "
            "allowlist",
        ),
        (
            model({"web": web(labels={"b": "1", "com.docker.x": "1"})}),
            "services.web.labels sets com.docker.x, and deadlift.* and com.docker.* labels are "
            "deadlift's and Docker's",
        ),
        (model({"web": web(security_opt=["no-new-privileges:true"])}), None),
        (
            model({"web": web(security_opt=["seccomp:unconfined"])}),
            "services.web.security_opt has seccomp:unconfined: only no-new-privileges is allowed",
        ),
        (
            model({"web": web(pull_policy="build")}),
            "services.web.pull_policy is build: deadlift runs images only, built and pushed by CI",
        ),
        (
            model({"web": web(cap_add=["NET_ADMIN"])}),
            "services.web.cap_add is set: added capabilities reach past the container",
        ),
        (
            model({"web": web(mac_address="x")}),
            "services.web.mac_address is set: it isn't on deadlift's allowlist",
        ),
        (
            model({"web": web()}, volumes={"data": {"name": "other_data"}}),
            "volumes.data.name is other_data: a volume named outside the project could be "
            "another App's",
        ),
        (
            model({"web": web()}, networks={"n": {"name": f"{PROJECT}_n", "external": True}}),
            "networks.n.external is true: an external network is outside the App's project",
        ),
        (
            model({"web": web()}, networks={"n": {"name": f"{PROJECT}_n", "driver": "bridge"}}),
            None,
        ),
        (
            model({"web": web()}, volumes={"v": {"name": f"{PROJECT}_v", "driver": "nfs"}}),
            "volumes.v.driver is nfs: only the default driver is allowed",
        ),
        (
            model({"web": web()}, networks={"n": {"ipam": {"config": [{"subnet": "x"}]}}}),
            "networks.n.ipam is set: the project's addresses are Docker's to choose",
        ),
        (
            model({"web": web()}, secrets={"s": {"name": "anything", "file": "/etc/shadow"}}),
            "secrets.s.file reads /etc/shadow from the Server: only inline content is allowed",
        ),
        (
            model({"web": web()}, volumes={"v": {"driver_opts": {"o": "bind"}}}),
            "volumes.v.driver_opts is set: driver options can mount the Server's files",
        ),
        (
            model({"web": web()}, models={"m": {}}),
            "models is set: it isn't on deadlift's allowlist",
        ),
    ],
)
def test_the_allowlist_names_the_first_key_it_refuses(
    config: dict[str, Any], problem: str | None
) -> None:
    assert allowlist_problem(config) == problem


def test_top_level_keys_come_first_in_their_sorted_order() -> None:
    config = model({"web": web(privileged=True)}, zeta=1, alpha=2, **{"x-other": 1})
    assert allowlist_problem(config) == "alpha is set: it isn't on deadlift's allowlist"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "compose.yaml"
    path.write_text(text)
    return path


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("services:\n  web:\n    image: x\n", None),
        ("include:\n  - other.yaml\n", "include"),
        (
            "services:\n  web:\n    env_file: /srv/deadlift/apps/b/feature.env\n",
            "services.web.env_file",
        ),
        ("x: &s {extends: base}\nservices:\n  web: *s\n", "services.web.extends"),
        ("services:\n  web: !reset\n    label_file: f\n", "services.web.label_file"),
        ("services: {}\n---\nconfigs:\n  c:\n    file: /etc/passwd\n", "configs.c.file"),
    ],
)
def test_keys_that_read_a_file_on_the_server_are_found_before_compose_runs(
    tmp_path: Path, text: str, key: str | None
) -> None:
    assert file_reading_key(write(tmp_path, text)) == key


def test_limits_come_from_app_conf_with_defaults() -> None:
    assert parse_limits([("memory", "512m"), ("pids", "100")]) == Limits(
        memory=512 * 1024 * 1024, cpus="1", pids=100
    )
    assert parse_limits([]) == Limits(memory=1024**3, cpus="1", pids=512)
    assert parse_limits([("memory", "12k"), ("cpus", "0.5")]) == Limits(12288, "0.5", 512)
    for broken in [("memory", "1t"), ("cpus", ".5"), ("pids", "0"), ("memory", "")]:
        assert parse_limits([broken]) is None


def test_the_override_keeps_a_lower_limit_of_the_templates_own_and_publishes_the_route() -> None:
    config = model(
        {
            "web": web(mem_limit="268435456", cpus=2, pids_limit=None),
            "worker": web(cpus=0.25),
        },
        volumes={"data": {"name": f"{PROJECT}_data"}},
        **{"x-deadlift": {"route": "web:8080"}},
    )
    limits = Limits(memory=1024**3, cpus="1", pids=512)
    document = override_document(config, port="20000", app="shop", environment="dev", limits=limits)
    labels = {"deadlift.app": "shop", "deadlift.environment": "dev"}
    hardening = {
        "security_opt": ["no-new-privileges:true"],
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
        "labels": labels,
    }
    assert json.loads(document) == {
        "services": {
            "web": {
                "mem_limit": 268435456,
                "cpus": 1,
                "pids_limit": 512,
                **hardening,
                "ports": [{"host_ip": "127.0.0.1", "published": "20000", "target": 8080}],
            },
            "worker": {"mem_limit": 1073741824, "cpus": 0.25, "pids_limit": 512, **hardening},
        },
        "volumes": {"data": {"labels": labels}},
    }
    assert document.startswith('{\n  "services": {\n    "web": {\n      "mem_limit": 268435456,')


@pytest.mark.parametrize("entry", ["db", "db:ro", "db:rw"])
def test_volumes_from_another_service_of_the_template_is_allowed(entry: str) -> None:
    assert allowlist_problem(model({"web": web(volumes_from=[entry]), "db": web()})) is None


def test_volumes_from_anything_but_a_service_of_the_template_is_refused() -> None:
    problem = allowlist_problem(model({"web": web(volumes_from=["other:ro"]), "db": web()}))
    assert problem is not None
    assert problem.startswith("services.web.volumes_from names other:ro")


def test_a_model_not_shaped_as_compose_makes_one_cannot_be_checked() -> None:
    with pytest.raises(Unchecked):
        allowlist_problem(model({"web": web(network_mode=3)}))
