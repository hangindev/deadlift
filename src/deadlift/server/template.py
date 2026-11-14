"""deadlift's template check: what an App's Compose template may hold (ADR 0001), and the override
deadlift adds to it. Bootstrap installs it on the Server beside the deploy script, owned by root,
at /usr/local/lib/deadlift/template, and only the deploy script runs it. It never starts,
stops or changes anything: it reads a template and prints JSON.

Usage:
  template check <dir> <project>
  template override <dir> <project> --port <port> --app <app> --environment <env>
      --limits <app.conf>

  <dir>      Holds compose.yaml, the App's template, and the variables it's deployed with:
             vars.env, and secrets.env when the App has a secrets file. vars.env wins.
  <project>  The Environment's Compose project, deadlift-<app>-<environment>.

check     Prints the template as `docker compose config` normalises it, when deadlift will
          run it. The App's routed service is its `x-deadlift.route: <service>:<port>`, and
          has a Compose healthcheck that isn't disabled (ADR 0002).
override  Checks the template the same way, then prints deadlift's override for it, the
          Compose file merged over the template to run it:
            - the routed service published on 127.0.0.1:<port>, and nothing else published;
            - every service limited to the App's memory, CPUs and processes, from the
              `memory`, `cpus` and `pids` settings in its app.conf, or to the template's own
              limit where that's lower;
            - no-new-privileges and capped json-file logs on every service;
            - the labels deadlift.app and deadlift.environment on every service and volume.

How a template is checked:
  1. Before Compose reads it, the YAML itself must not use include, extends, env_file,
     label_file, or configs or secrets read from a file. Compose reads those files from the
     Server as root, some of them while normalising, and they could be another App's secrets
     files. (A bind mount can only be told once Compose has interpolated the template, and
     normalising doesn't read its source, so step 3 refuses it.)
  2. `docker compose config` normalises it, with nothing of this process's environment, or its
     caller's, visible: only secrets.env and vars.env.
  3. Every key of the normalised model must be on the allowlist below. Keys not on it are
     refused, so a key Compose adds later is refused until it's judged.

The deploy script calls :func:`check` and :func:`override` in its own process; the launcher runs
:func:`main`.

Every reason line starts with `deadlift: `.

Exit codes: 0 done, 1 failed, 2 refused (the template, or bad usage).
"""

import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

import yaml

from deadlift.core.outcome import Exit, Stop, refuse_usage, run_main
from deadlift.core.process import Env, Stream, run, scrubbed_env
from deadlift.server.text import jq_text, last_line, read_settings

USAGE = (
    "Usage: template check <dir> <project>\n"
    "Usage: template override <dir> <project> --port <port> --app <app> "
    "--environment <env> --limits <app.conf>\n"
)

# What an App gets when its app.conf doesn't say, per container.
DEFAULT_MEMORY = "1g"
DEFAULT_CPUS = "1"
DEFAULT_PIDS = "512"


def _refuse(why: str) -> NoReturn:
    raise Stop(Exit.REFUSED, f"{why}. Nothing was changed")


def _fail(why: str) -> NoReturn:
    raise Stop(Exit.FAILED, f"{why}. Nothing was changed")


def _usage(why: str) -> NoReturn:
    refuse_usage(why, USAGE)


def child_env() -> Env:
    """What `docker compose` sees of the environment: PATH, HOME and DOCKER_HOST, nothing else,
    so neither this process's variables nor its caller's reach a template's ${VAR}."""
    env = scrubbed_env(keep=("PATH", "HOME", "DOCKER_HOST"))
    if "HOME" not in env.variables:
        env = env.with_vars({"HOME": "/root"})
    return env


# Step 1: keys that would make Compose read a file on the Server.


class _Loader(yaml.SafeLoader):
    pass


def _any_local_tag(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> object:
    # Compose's own tags, such as !reset and !override, read as the value they tag.
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(cast(yaml.ScalarNode, node))


_Loader.add_multi_constructor("!", _any_local_tag)  # pyright: ignore[reportUnknownMemberType]


class NotYaml(Exception):
    """The template isn't YAML deadlift can read; its message is the reason, on one line."""


def file_reading_key(path: Path) -> str | None:
    """The first place in the template's YAML that would make Compose read a file on the Server,
    as a path such as ``include`` or ``services.web.env_file``, or None. Anchors and merge keys
    are resolved, as Compose resolves them, and every document of the file counts, as Compose
    merges them. Raises :class:`NotYaml` for a file that isn't YAML.
    """
    try:
        with path.open(encoding="utf-8") as file:
            documents: list[object] = list(yaml.load_all(file, Loader=_Loader))
    except (yaml.YAMLError, UnicodeDecodeError) as error:
        raise NotYaml(str(error).replace("\n", " ")) from error
    except Exception as error:  # as the last line of Python's traceback would say
        raise NotYaml(f"{type(error).__name__}: {error}") from error
    for document in documents:
        if not isinstance(document, dict):
            continue
        top = cast(dict[object, object], document)
        if "include" in top:
            return "include"
        for kind in ("configs", "secrets"):
            resources = top.get(kind)
            if not isinstance(resources, dict):
                continue
            for name, resource in cast(dict[object, object], resources).items():
                if isinstance(resource, dict) and "file" in resource:
                    return f"{kind}.{name}.file"
        services = top.get("services")
        if not isinstance(services, dict):
            continue
        for name, service in cast(dict[object, object], services).items():
            if not isinstance(service, dict):
                continue
            for key in ("extends", "env_file", "label_file"):
                if key in service:
                    return f"services.{name}.{key}"
    return None


# Step 3: the allowlist, over the normalised model.

_WHY = {
    "privileged": "it gives the container the whole Server",
    "cap_add": "added capabilities reach past the container",
    "devices": "it reaches the Server's devices",
    "device_cgroup_rules": "it reaches the Server's devices",
    "gpus": "it reaches the Server's devices",
    "build": "deadlift runs images only, built and pushed by CI",
    "ports": "only deadlift publishes, and only the routed service, on the Server's loopback",
    "use_api_socket": "it hands the container the Docker socket",
    "container_name": "a container name is shared by every App on the Server",
    "external_links": "it reaches containers outside the App's project",
    "userns_mode": "it changes the user namespace",
    "uts": "it shares the Server's UTS namespace",
    "cgroup_parent": "it moves the container out of deadlift's limits",
    "runtime": "it changes how the container is run",
    "sysctls": "it changes kernel settings",
    "oom_score_adj": "it changes how the Server picks what to stop when memory runs out",
    "oom_kill_disable": "it changes how the Server picks what to stop when memory runs out",
    "deploy": "deadlift sets the limits, from the App's settings",
}


def _why(key: str) -> str:
    return _WHY.get(key, "it isn't on deadlift's allowlist")


# Keys allowed as they are.
_PLAIN_SERVICE_KEYS = frozenset(
    {
        "attach",
        "cap_drop",
        "command",
        "configs",
        "cpus",
        "depends_on",
        "dns",
        "dns_opt",
        "dns_search",
        "domainname",
        "entrypoint",
        "environment",
        "expose",
        "extra_hosts",
        "group_add",
        "healthcheck",
        "hostname",
        "image",
        "init",
        "links",
        "mem_limit",
        "pids_limit",
        "platform",
        "profiles",
        "read_only",
        "restart",
        "scale",
        "secrets",
        "shm_size",
        "stdin_open",
        "stop_grace_period",
        "stop_signal",
        "tmpfs",
        "tty",
        "user",
        "working_dir",
    }
)
_MOUNT_KEYS = frozenset({"type", "source", "target", "read_only", "volume", "tmpfs", "consistency"})
_SERVICE_NETWORK_KEYS = frozenset({"aliases", "priority", "gw_priority"})
_TOP_LEVEL_KEYS = frozenset({"name", "services", "networks", "volumes", "configs", "secrets"})


class Unchecked(Exception):
    """The template's normalised model has a shape the allowlist doesn't know, so it couldn't be
    checked. :func:`check` refuses it."""


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise Unchecked(f"{value!r} isn't what the allowlist expects there")
    return cast(dict[str, object], value)


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise Unchecked(f"{value!r} isn't what the allowlist expects there")
    return cast(list[object], value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise Unchecked(f"{value!r} isn't what the allowlist expects there")
    return value


def _get(value: object, key: str) -> object:
    """jq's ``.key``: null on null, an error on anything but an object."""
    if value is None:
        return None
    return _object(value).get(key)


def _truthy(value: object) -> bool:
    return value is not None and value is not False


def _namespace_ok(value: object, allowed: Sequence[str]) -> bool:
    return value in allowed or _string(value).startswith("service:")


def _service_problems(
    service: str, keys: dict[str, object], services: dict[str, object]
) -> list[str]:
    found: list[str] = []
    for key, value in keys.items():
        path = f"services.{service}.{key}"
        if key in _PLAIN_SERVICE_KEYS:
            continue
        if key == "privileged":
            if value is True:
                found.append(f"{path} is true: {_why(key)}")
        elif key == "network_mode":
            if _namespace_ok(value, ["none"]):
                continue
            if _string(value).startswith("container:"):
                found.append(
                    f"{path} is {value}: another container's network (container:) reaches "
                    "outside the App's project"
                )
            else:
                found.append(
                    f"{path} is {value}: only the project's own networks are allowed, never the "
                    "host's network or a shared one"
                )
        elif key in ("pid", "ipc", "cgroup"):
            if _namespace_ok(value, ["private", "shareable", "none"]):
                continue
            if _string(value).startswith("container:"):
                found.append(
                    f"{path} is {value}: another container's namespace (container:) reaches "
                    "outside the App's project"
                )
            else:
                found.append(f"{path} is {value}: it shares the Server's {key.upper()} namespace")
        elif key == "volumes":
            for mount_value in _array(value):
                mount = _object(mount_value)
                kind = mount.get("type")
                source = mount.get("source")
                if kind == "bind" and "docker.sock" in _string(source if _truthy(source) else ""):
                    found.append(
                        f"{path} mounts the Docker socket, {jq_text(source)}, which reaches every "
                        "App on the Server"
                    )
                elif kind == "bind":
                    found.append(
                        f"{path} bind-mounts {jq_text(source)} from the Server: only named volumes "
                        "and tmpfs are allowed"
                    )
                elif kind in ("volume", "tmpfs"):
                    extra = sorted(set(mount) - _MOUNT_KEYS)
                    if extra:
                        found.append(f"{path} sets {extra[0]} on a mount: {_why(extra[0])}")
                else:
                    found.append(
                        f"{path} mounts a {jq_text(kind)}: only named volumes and tmpfs are allowed"
                    )
        elif key == "volumes_from":
            for entry_value in _array(value):
                entry = _string(entry_value)
                if entry.startswith("container:"):
                    found.append(
                        f"{path} names {entry}: another container (container:) reaches outside "
                        "the App's project"
                    )
                elif re.sub(r":(ro|rw)$", "", entry) not in services:
                    found.append(
                        f"{path} names {entry}, which isn't a service of the template: only the "
                        "project's own services' volumes are allowed"
                    )
        elif key == "networks":
            for network in _object(value).values():
                extra = sorted(
                    set(_object(network if _truthy(network) else {})) - _SERVICE_NETWORK_KEYS
                )
                if extra:
                    found.append(f"{path} sets {extra[0]} on a network: {_why(extra[0])}")
        elif key == "labels":
            for label in sorted(_object(value)):
                if label.startswith(("deadlift.", "com.docker.")):
                    found.append(
                        f"{path} sets {label}, and deadlift.* and com.docker.* labels are "
                        "deadlift's and Docker's"
                    )
        elif key == "security_opt":
            for option_value in _array(value):
                option = _string(option_value)
                if not re.search(r"^no-new-privileges([:=]true)?$", option):
                    found.append(f"{path} has {option}: only no-new-privileges is allowed")
        elif key == "pull_policy":
            if value == "build":
                found.append(f"{path} is build: {_why('build')}")
        else:
            found.append(f"{path} is set: {_why(key)}")
    return found


def _resource_problems(
    kind: str, name: str, resource: object, allowed: Sequence[str], project: object
) -> list[str]:
    """A top-level volume, network, config or secret: its own keys, and its name, which must be
    the one Compose gives it in this project so it can never be another project's."""
    single = kind.removesuffix("s")
    if _get(resource, "external") is True:
        return [
            f"{kind}.{name}.external is true: an external {single} is outside the App's project"
        ]
    found: list[str] = []
    for key, value in _object(resource).items():
        path = f"{kind}.{name}.{key}"
        if key == "name":
            if kind in ("configs", "secrets") or value == f"{jq_text(project)}_{name}":
                continue
            found.append(
                f"{path} is {jq_text(value)}: a {single} named outside the project could be "
                "another App's"
            )
        elif key == "external":
            continue
        elif key == "driver_opts":
            found.append(f"{path} is set: driver options can mount the Server's files")
        elif key == "file":
            found.append(
                f"{path} reads {jq_text(value)} from the Server: only inline content is allowed"
            )
        elif key == "driver":
            if value == "local" or (kind == "networks" and value == "bridge"):
                continue
            found.append(f"{path} is {jq_text(value)}: only the default driver is allowed")
        elif key == "ipam":
            ipam = _object(value)
            config = ipam.get("config")
            driver = ipam.get("driver")
            if (
                not set(ipam) - {"config", "driver"}
                and len(_array(config if _truthy(config) else [])) == 0
                and (driver if _truthy(driver) else "default") == "default"
            ):
                continue
            found.append(f"{path} is set: the project's addresses are Docker's to choose")
        elif key in allowed:
            continue
        else:
            found.append(f"{path} is set: {_why(key)}")
    return found


def _entries(config: dict[str, object], key: str) -> dict[str, object]:
    value = config.get(key)
    return _object(value) if _truthy(value) else {}


def allowlist_problem(config: Mapping[str, object]) -> str | None:
    """The first key of the normalised model that the allowlist refuses, as its path, then why;
    or None when every key is allowed."""
    model = dict(config)
    found = [
        f"{key} is set: {_why(key)}"
        for key in sorted(model)
        if key not in _TOP_LEVEL_KEYS and not key.startswith("x-")
    ]
    services = _entries(model, "services")
    for service, keys in services.items():
        found += _service_problems(service, _object(keys), _object(model.get("services")))
    project = model.get("name")
    for kind, allowed in (
        ("volumes", ("labels",)),
        ("networks", ("labels", "internal")),
        ("configs", ("content", "environment", "labels")),
        ("secrets", ("content", "environment", "labels")),
    ):
        for name, resource in _entries(model, kind).items():
            found += _resource_problems(kind, name, resource, allowed, project)
    return found[0] if found else None


# Checking a template.


@dataclass(frozen=True)
class Checked:
    """A template deadlift will run: as `docker compose config` printed it, and parsed."""

    text: str
    config: dict[str, object]
    routed: str


def check(directory: Path, project: str) -> Checked:
    """Checks the template in ``directory`` for the Compose project ``project``. Raises
    :class:`Stop` (refused, with its reason) for a template deadlift won't run (ADR 0001,
    ADR 0002)."""
    compose = directory / "compose.yaml"
    variables = directory / "vars.env"
    if not (compose.is_file() and variables.is_file()):
        _usage(f"{directory} needs compose.yaml and vars.env")
    env_files: list[str] = []
    if (directory / "secrets.env").is_file():
        env_files += ["--env-file", str(directory / "secrets.env")]
    env_files += ["--env-file", str(variables)]

    try:
        key = file_reading_key(compose)
    except NotYaml as error:
        _refuse(f"the template isn't valid YAML: {error}")
    if key is not None:
        _refuse(
            f"the template uses {key}, which would read a file on the Server, and ADR 0001 "
            "refuses it"
        )

    done = run(
        [
            "docker",
            "compose",
            "--project-name",
            project,
            "--project-directory",
            str(directory),
            *env_files,
            "--file",
            str(compose),
            "config",
            "--format",
            "json",
            "--no-env-resolution",
        ],
        env=child_env(),
        stdout=Stream.CAPTURE,
        stderr=Stream.CAPTURE,
    )
    if not done.ok:
        _refuse(f"the template isn't valid Compose: {last_line(done.stderr)}")
    text = done.stdout.rstrip("\n")
    try:
        return _check_model(text)
    except (ValueError, Unchecked) as error:
        _refuse(
            "the template couldn't be checked: `docker compose config` gave it a shape "
            f"deadlift's allowlist doesn't know ({error})"
        )


def _check_model(text: str) -> Checked:
    """Checks the normalised model ``text``. Raises :class:`Stop` for a template deadlift won't
    run, and :class:`Unchecked` or ValueError for one it can't read."""
    config = _object(json.loads(text))
    problem = allowlist_problem(config)
    if problem is not None:
        _refuse(f"the template's {problem}, and ADR 0001 refuses it")

    route_value = _get(config.get("x-deadlift"), "route")
    route = jq_text(route_value).rstrip("\n") if _truthy(route_value) else ""
    if not route:
        _refuse(
            "the template has no x-deadlift.route. Name the service deadlift routes to as "
            "`x-deadlift.route: <service>:<port>`"
        )
    matched = re.fullmatch(r"([a-zA-Z0-9][a-zA-Z0-9_.-]*):([0-9]{1,5})", route)
    if matched is None:
        _refuse(f"x-deadlift.route is '{route}', and it must be <service>:<port>")
    routed = matched[1]
    service = _get(config.get("services"), routed)
    if not _truthy(service):
        _refuse(f"x-deadlift.route names the service {routed}, and the template has none")
    healthcheck = _get(service, "healthcheck")
    if healthcheck is None:
        _refuse(
            f"the routed service {routed} has no healthcheck in the template, and deadlift "
            "judges an Environment by it (ADR 0002)"
        )
    test = _get(healthcheck, "test")
    tests = cast(list[object], test) if isinstance(test, list) else None
    if _get(healthcheck, "disable") is True or (tests is not None and tests[:1] == ["NONE"]):
        _refuse(
            f"the routed service {routed} has its healthcheck disabled, and deadlift "
            "judges an Environment by it (ADR 0002)"
        )
    return Checked(text=text, config=config, routed=routed)


# The override.


@dataclass(frozen=True)
class Limits:
    """The App's limits per container: memory in bytes, CPUs as app.conf writes them, and
    processes."""

    memory: int
    cpus: str
    pids: int


def _bytes(size: str) -> int | None:
    """A size such as 512m or 1g, in bytes."""
    matched = re.fullmatch(r"([0-9]+)([kmg]?)", size)
    if matched is None:
        return None
    return int(matched[1]) * {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[matched[2]]


def parse_limits(settings: Sequence[tuple[str, str]]) -> Limits | None:
    """The limits that app.conf's ``settings`` give, with the defaults for those missing; None
    when one is broken."""
    memory, cpus, pids = DEFAULT_MEMORY, DEFAULT_CPUS, DEFAULT_PIDS
    for key, value in settings:
        match key:
            case "memory":
                memory = value
            case "cpus":
                cpus = value
            case "pids":
                pids = value
            case _:
                pass
    in_bytes = _bytes(memory)
    if (
        in_bytes is None
        or re.fullmatch(r"[0-9]+(\.[0-9]+)?", cpus) is None
        or re.fullmatch(r"[1-9][0-9]*", pids) is None
    ):
        return None
    return Limits(memory=in_bytes, cpus=cpus, pids=int(pids))


_JSON_NUMBER = re.compile(r"-?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def _own_limit(value: object) -> int | float:
    """jq's ``tonumber? // 0``: the template's own limit, or 0 when it has none."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return value
    if isinstance(value, str) and _JSON_NUMBER.fullmatch(value):
        number = float(value)
        return int(number) if number.is_integer() and abs(number) < 2**53 else number
    return 0


def _lower(own: object, limit: int | float) -> int | float:
    number = _own_limit(own)
    return number if 0 < number < limit else limit


def override_document(
    config: Mapping[str, object], *, port: str, app: str, environment: str, limits: Limits
) -> str:
    """deadlift's override for the checked template ``config``, as JSON: each limit the
    template's own where that's set and lower, else the App's."""
    route = jq_text(_get(config.get("x-deadlift"), "route"))
    matched = re.search(r"^(.+):([0-9]+)$", route)
    if matched is None:  # pragma: no cover - check() let only <service>:<port> through
        raise ValueError(f"x-deadlift.route is {route!r}")
    routed, target = matched[1], int(matched[2])
    labels = {"deadlift.app": app, "deadlift.environment": environment}
    cpus: int | float = float(limits.cpus) if "." in limits.cpus else int(limits.cpus)
    services: dict[str, dict[str, object]] = {}
    for name, service_value in _object(config.get("services")).items():
        service = _object(service_value)
        services[name] = {
            "mem_limit": _lower(service.get("mem_limit"), limits.memory),
            "cpus": _lower(service.get("cpus"), cpus),
            "pids_limit": _lower(service.get("pids_limit"), limits.pids),
            "security_opt": ["no-new-privileges:true"],
            "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
            "labels": labels,
        }
    services.setdefault(routed, {})["ports"] = [
        {"host_ip": "127.0.0.1", "published": port, "target": target}
    ]
    volumes = config.get("volumes")
    document = {
        "services": services,
        "volumes": {name: {"labels": labels} for name in _object(volumes)}
        if _truthy(volumes)
        else {},
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def override(
    directory: Path, project: str, *, port: str, app: str, environment: str, limits: Path
) -> str:
    """Checks the template in ``directory``, then gives deadlift's override for it. Raises
    :class:`Stop` as :func:`check` does, or failed when the App's limits are broken."""
    parsed = parse_limits(read_settings(limits))
    if parsed is None:
        _fail(f"{app}'s limits in the Server registry are broken. Re-run `deadlift app add {app}`")
    checked = check(directory, project)
    return override_document(
        checked.config, port=port, app=app, environment=environment, limits=parsed
    )


def _override_command(argv: Sequence[str]) -> None:
    directory, project, rest = argv[0], argv[1], list(argv[2:])
    values = {"--port": "", "--app": "", "--environment": "", "--limits": ""}
    while rest:
        flag = rest[0]
        if flag not in values:
            _usage(f"unknown argument '{flag}' for override")
        if len(rest) < 2:
            _usage(f"{flag} needs a value")
        values[flag] = rest[1]
        rest = rest[2:]
    port, app, environment, limits = (values[flag] for flag in values)
    if not (re.fullmatch(r"[0-9]+", port) and app and environment and Path(limits).is_file()):
        _usage("override needs --port, --app, --environment and --limits")
    _write(
        override(
            Path(directory),
            project,
            port=port,
            app=app,
            environment=environment,
            limits=Path(limits),
        )
    )


def _write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _main(argv: Sequence[str]) -> None:
    os.umask(0o077)
    command = argv[0] if argv else ""
    commands: dict[str, Callable[[], None]] = {
        "check": lambda: _write(check(Path(argv[1]), argv[2]).text + "\n"),
        "override": lambda: _override_command(argv[1:]),
    }
    if command == "check" and len(argv) != 3:
        _usage("check needs <dir> <project>")
    if command == "override" and len(argv) < 3:
        _usage("override needs <dir> <project>")
    if command not in commands:
        _usage(f"'{command}' isn't a command: check or override")
    commands[command]()


def main(argv: Sequence[str]) -> int:
    return run_main(lambda: _main(argv))
