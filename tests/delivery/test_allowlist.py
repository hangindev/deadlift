"""What an App's Compose template may hold (ADR 0001): anything that reaches past the App's own
Compose project is refused before anything stops."""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

from tests.support.harness import Harness, Server, example_template, get, matches

WEB = "\n  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n"


def in_web(lines: str) -> str:
    """The example template with `lines` added to its routed service, web."""
    template = example_template()
    if WEB not in template:
        raise AssertionError("the example template's web service has moved")
    return template.replace(WEB, f"{WEB}{lines}", 1)


def with_probe(lines: str) -> str:
    """The example template with one more service, `probe`, running the web image with
    `lines`."""
    return example_template().replace(
        "services:\n", "services:\n  probe:\n    image: ${DEADLIFT_IMAGE_WEB}\n" + lines, 1
    )


def at_top_level(text: str) -> str:
    """The example template with `text` added at its top level."""
    return f"{example_template()}\n{text}"


def using(kind: Literal["secrets", "configs"], definition: str) -> str:
    """The example template, with `web` also using `kind` (secrets or configs) `shared`, defined
    at the top level as `definition`."""
    return at_top_level(f"{kind}:\n  shared:\n{definition}").replace(
        WEB, f"\n  web:\n    image: ${{DEADLIFT_IMAGE_WEB}}\n    {kind}: [shared]\n", 1
    )


def with_volume(definition: str) -> str:
    """The example template with its named volume `data` defined as `definition`."""
    return re.sub(
        r"^volumes:\n {2}data:\n",
        lambda _: f"volumes:\n  data:\n{definition}",
        example_template(),
        count=1,
        flags=re.M,
    )


REFUSALS: list[tuple[str, Callable[[], str], str]] = [
    # Host files, which Compose would read as root.
    (
        "a bind mount",
        lambda: with_probe("    volumes:\n      - /etc:/host-etc:ro\n"),
        r"volumes.*/etc",
    ),
    ("env_file", lambda: in_web("    env_file: /etc/environment\n"), r"env_file"),
    (
        "env_file through a YAML merge key",
        lambda: f"x-files: &files\n  env_file: /etc/environment\n{example_template()}".replace(
            WEB, "\n  web:\n    <<: *files\n    image: ${DEADLIFT_IMAGE_WEB}\n", 1
        ),
        r"env_file",
    ),
    ("include", lambda: at_top_level("include:\n  - /etc/deadlift/other.yaml\n"), r"include"),
    (
        "extends",
        lambda: in_web("    extends:\n      file: /etc/deadlift/other.yaml\n      service: web\n"),
        r"extends",
    ),
    ("label_file", lambda: in_web("    label_file: /etc/hostname\n"), r"label_file"),
    (
        "a secret read from a file",
        lambda: using("secrets", "    file: /etc/shadow\n"),
        r"secrets\.shared\.file",
    ),
    (
        "a config read from a file",
        lambda: using("configs", "    file: /etc/passwd\n"),
        r"configs\.shared\.file",
    ),
    (
        "a named volume with driver_opts",
        lambda: with_volume("    driver_opts: {type: none, o: bind, device: /etc}\n"),
        r"driver_opts",
    ),
    # Privileges and the host's namespaces.
    ("a privileged container", lambda: in_web("    privileged: true\n"), r"privileged"),
    ("added capabilities", lambda: in_web("    cap_add: [SYS_ADMIN]\n"), r"cap_add"),
    ("devices", lambda: in_web("    devices: [/dev/fuse:/dev/fuse]\n"), r"devices"),
    (
        "a loosened security_opt",
        lambda: in_web("    security_opt: [seccomp=unconfined]\n"),
        r"security_opt",
    ),
    ("the host's network", lambda: in_web("    network_mode: host\n"), r"network_mode"),
    ("the host's PID namespace", lambda: in_web("    pid: host\n"), r"pid"),
    ("the host's IPC namespace", lambda: in_web("    ipc: host\n"), r"ipc"),
    ("the host's UTS namespace", lambda: in_web("    uts: host\n"), r"uts"),
    ("the host's user namespace", lambda: in_web("    userns_mode: host\n"), r"userns_mode"),
    # Networks, containers and volumes from outside the project.
    (
        "an external network",
        lambda: at_top_level(
            "networks:\n  shared:\n    external: true\n    name: bridge\n"
        ).replace(WEB, "\n  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n    networks: [shared]\n", 1),
        r"networks\.shared\.external",
    ),
    (
        "another container's network",
        lambda: in_web('    network_mode: "container:other"\n'),
        r"container:",
    ),
    (
        "volumes_from another container",
        lambda: in_web('    volumes_from: ["container:other"]\n'),
        r"volumes_from",
    ),
    (
        "a volume named for another project",
        lambda: with_volume("    name: deadlift-other-dev_data\n"),
        r"volumes\.data\.name",
    ),
    ("an external volume", lambda: with_volume("    external: true\n"), r"volumes\.data\.external"),
    # The Docker socket, in any form.
    (
        "the Docker socket",
        lambda: with_probe("    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock\n"),
        r"Docker socket",
    ),
    ("use_api_socket", lambda: in_web("    use_api_socket: true\n"), r"use_api_socket"),
    # Images only, and only deadlift publishes.
    ("build", lambda: in_web("    build: .\n"), r"build"),
    ("published ports", lambda: in_web('    ports: ["8080:3000"]\n'), r"ports"),
    ("a container_name", lambda: in_web("    container_name: web\n"), r"container_name"),
]

POSTGRES_IMAGE = (
    "postgres:17.6-alpine@sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94"
)


@pytest.mark.usefixtures("remove_servers_after_class")
class TestWhatATemplateMayHold:
    def test_inline_configs_tmpfs_and_shm_size_are_allowed_and_the_official_postgres_image_starts(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        app = server.apps["a"].name
        # Postgres runs the inline config as its first-start script, and is healthy only once
        # the table that script makes is there. web waits for it.
        template = example_template().replace(
            "services:\n",
            f"""services:
  postgres:
    image: {POSTGRES_IMAGE}
    environment:
      POSTGRES_PASSWORD: example
    configs:
      - source: schema
        target: /docker-entrypoint-initdb.d/schema.sql
    tmpfs:
      - /tmp
    shm_size: 256m
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "psql -h 127.0.0.1 -U postgres -tAc 'select 1 from marker'"]
      interval: 1s
      retries: 120
""",  # noqa: S608
            1,
        )
        template = template.replace(
            "    depends_on:\n",
            "    depends_on:\n      postgres:\n        condition: service_healthy\n",
            1,
        )
        template = re.sub(
            r"^volumes:\n",
            lambda _: (
                "configs:\n  schema:\n    content: |\n      create table marker (id int);\n"
                "\nvolumes:\n  pgdata:\n"
            ),
            template,
            count=1,
            flags=re.M,
        )

        result = server.deliver(
            app, ["deploy", "feature-1", "--image", f"web={harness.image()}"], template=template
        )

        assert result.outcome == "done", result.output
        assert get(server.slot_port("a", 1), "/version") == "1"
        assert matches(r"-postgres-1", server.running_containers(app))

    @pytest.mark.parametrize("entry", ["migrate", "migrate:ro"])
    def test_volumes_from_another_service_of_the_template_is_allowed(
        self, harness: Harness, entry: str
    ) -> None:
        server = harness.server()
        app = server.apps["a"].name

        result = server.deliver(
            app,
            ["deploy", "feature-1", "--image", f"web={harness.image()}"],
            template=with_probe(f'    volumes_from: ["{entry}"]\n'),
        )

        assert result.outcome == "done", result.output
        assert get(server.slot_port("a", 1), "/version") == "1"
        assert matches(r"-probe-1", server.running_containers(app))


WAYS: list[tuple[str, Callable[[Path], str]]] = [
    ("env_file", lambda file: in_web(f"    env_file: {file}\n")),
    ("a bind mount", lambda file: with_probe(f"    volumes:\n      - {file}:/stolen.env:ro\n")),
    ("a config read from a file", lambda file: using("configs", f"    file: {file}\n")),
    ("a secret read from a file", lambda file: using("secrets", f"    file: {file}\n")),
]


@pytest.mark.usefixtures("remove_servers_after_class")
class TestAnotherAppsSecretsFile:
    @pytest.mark.parametrize("template", [way for _, way in WAYS], ids=[name for name, _ in WAYS])
    def test_cant_be_read_by_a_template_through(
        self, harness: Harness, template: Callable[[Path], str]
    ) -> None:
        server = harness.server()
        a, b = server.apps["a"].name, server.apps["b"].name
        file = server.secrets(a, "feature", "A_ONLY=a's own secret\n")

        result = server.deliver(
            b, ["deploy", "feature-1", "--image", f"web={harness.image()}"], template=template(file)
        )

        assert result.code == 2, result.output
        assert matches(r"^deadlift: .*ADR 0001", result.reason)
        assert not matches(r"a's own secret", result.output)
        assert server.containers(b) == ""


@dataclass(frozen=True)
class Running:
    """One Feature Environment, up throughout, which every refused delivery targets."""

    server: Server
    app: str
    containers: str


def container_ids(server: Server, name: str) -> str:
    return "\n".join(sorted(server.running_containers(name).split("\n")))


@pytest.fixture(scope="class")
def running(harness: Harness) -> Iterator[Running]:
    server = harness.server()
    app = server.apps["a"].name
    result = server.deliver(
        app, ["deploy", "feature-1", "--image", f"web={harness.image('healthy', 1)}"]
    )
    assert result.outcome == "done", result.output
    yield Running(server, app, container_ids(server, app))
    harness.remove_servers()


class TestATemplateAdr0001Refuses:
    @pytest.mark.parametrize(
        ("template", "key"),
        [(template, key) for _, template, key in REFUSALS],
        ids=[name for name, _, _ in REFUSALS],
    )
    def test_is_refused_exit_2_naming_the_key_and_the_running_environment_is_unchanged(
        self, harness: Harness, running: Running, template: Callable[[], str], key: str
    ) -> None:
        server, app = running.server, running.app
        mappings = server.serve_mappings()

        result = server.deliver(
            app,
            ["deploy", "feature-1", "--image", f"web={harness.image('healthy', 2)}"],
            template=template(),
        )

        assert result.code == 2, result.output
        assert result.outcome == "failed"
        assert matches(r"^deadlift: ", result.reason)
        assert matches(key, result.reason)
        assert matches(r"ADR 0001", result.reason)
        assert container_ids(server, app) == running.containers
        assert get(server.slot_port("a", 1), "/version") == "1"
        assert server.serve_mappings() == mappings
