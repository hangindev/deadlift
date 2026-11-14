"""The delivery tests: a delivery end to end.

A test delivers as an App's CI would, through the `deliver` client script, to a fake Server: an
`ssh` shim logs in as the App's deploy user, a fake `sudo` allows only the entry command, and the
entry command runs the deploy script installed on the fake Server against real Docker. Around it:
a fake `tailscale`, a local registry holding the example App's images, and a temporary state
directory set up for two Apps.

This machine may be shared, and several runs may overlap. Everything a run creates carries one
random prefix, `deadlift-test-<hex>`, and cleanup removes only what that prefix names. Ports are
picked free at runtime.
"""

import contextlib
import json
import os
import random
import re
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Literal, cast

REPO = Path(__file__).resolve().parents[2]
FAKES = REPO / "test" / "fakes"
EXAMPLE_APP = REPO / "tests" / "delivery" / "example_app"
REGISTRY_IMAGE = (
    "registry:3@sha256:38907510915c3f1604c7e83d6ab602ab3020d092f73bdaed8696773693ff768c"
)
# The Server's Python and its launchers, as bootstrap installs them (ADR 0004).
INSTALL = (
    "import sys; sys.path.insert(0, sys.argv[1]); from deadlift.bootstrap.install import main; "
    "sys.exit(main(sys.argv[2:]))"
)

type ImageKind = Literal["healthy", "unhealthy", "failing-migration"]
"""The example App's images: healthy, one whose health check never passes, and one whose
migration fails."""

type AppKey = Literal["a", "b"]


class TimeLimitExceeded(Exception):
    """What runs inside `time_limit` ran past it."""


@contextlib.contextmanager
def time_limit(seconds: float) -> Generator[None]:
    """Stops what runs inside, from the main thread, once it has run for `seconds`."""

    def expire(_signum: int, _frame: FrameType | None) -> None:
        raise TimeLimitExceeded(f"ran past its time limit of {seconds:g} s")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def parse_github_output(text: str) -> dict[str, str]:
    """Parses what a step wrote to $GITHUB_OUTPUT, in its `name=value` and heredoc forms."""
    outputs: dict[str, str] = {}
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        heredoc = re.fullmatch(r"([^=<]+)<<(.+)", line)
        if heredoc:
            name, marker = heredoc.group(1), heredoc.group(2)
            value: list[str] = []
            i += 1
            while i < len(lines) and lines[i] != marker:
                value.append(lines[i])
                i += 1
            outputs[name] = "\n".join(value)
        elif "=" in line:
            name, _, value_text = line.partition("=")
            outputs[name] = value_text
        i += 1
    return outputs


def shell_quote(word: str) -> str:
    """Quotes a word for the remote shell, as `printf %q` would."""
    if re.fullmatch(r"[\w@%+=:,./-]+", word, flags=re.ASCII):
        return word
    return "'" + word.replace("'", "'\\''") + "'"


def matches(pattern: str, text: str | None, flags: int = 0) -> bool:
    """Whether `text` is there and `pattern` is found in it."""
    return text is not None and re.search(pattern, text, flags) is not None


def docker(*args: str) -> str:
    """Runs `docker`, returning its stdout, trimmed; raises when it fails."""
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} exited {result.returncode}: {result.stderr}")
    return result.stdout.strip()


def _docker_ok(*args: str) -> bool:
    try:
        docker(*args)
    except RuntimeError:
        return False
    return True


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return cast(tuple[str, int], listener.getsockname())[1]


def is_free(port: int) -> bool:
    """Whether a loopback port is free right now."""
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def free_port_block(taken: Sequence[int]) -> int:
    """A block of 100 loopback ports, as `deadlift app add` gives an App, whose Environments'
    ports (the base, and base + 10s for each Slot s) are all free and not in `taken`."""
    while True:
        base = 30000 + 100 * random.randrange(300)  # noqa: S311
        if base in taken:
            continue
        if all(is_free(base + 10 * slot) for slot in range(10)):
            return base


def require_tools() -> None:
    for tool in ("flock", "jq"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"The delivery tests need {tool}, which every Server has")
    check = subprocess.run(["python3", "-I", "-c", "import yaml"], capture_output=True, check=False)
    if check.returncode != 0:
        raise RuntimeError(
            "The delivery tests need python3 with PyYAML (python3-yaml), which every Server has"
        )


@dataclass(frozen=True)
class Result:
    code: int
    output: str
    """Its stdout and stderr together."""


@dataclass(frozen=True)
class Delivery:
    """What `deliver` reports, as an App's workflow reads it."""

    code: int
    output: str
    outcome: str | None
    reason: str | None
    url: str | None
    environment: str | None
    swept: str | None
    summary: str
    """What it wrote to the job summary."""


@dataclass(frozen=True)
class Limits:
    """An App's limits for each of its containers, as `deadlift app add` sets them: memory as
    Docker writes it (`512m`), CPUs, and processes."""

    memory: str | None = None
    cpus: str | None = None
    pids: int | None = None

    def lines(self) -> str:
        values = {"memory": self.memory, "cpus": self.cpus, "pids": self.pids}
        return "".join(f"{name}={value}\n" for name, value in values.items() if value is not None)


@dataclass(frozen=True)
class ServeMapping:
    service: str
    port: int
    target: str


@dataclass
class App:
    name: str
    port_base: int


def collect(
    argv: Sequence[str], *, env: Mapping[str, str], cwd: Path | None = None, input: bytes = b""
) -> Result:
    """Runs a command, collecting its stdout and stderr together."""
    completed = subprocess.run(
        list(argv),
        env=dict(env),
        cwd=cwd,
        input=input,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return Result(completed.returncode, completed.stdout.decode(errors="replace"))


def example_template() -> str:
    """The example App's Compose template, as its repo holds it."""
    return (EXAMPLE_APP / "compose.yaml").read_text()


def _docker_host() -> dict[str, str]:
    host = os.environ.get("DOCKER_HOST")
    return {"DOCKER_HOST": host} if host else {}


class Server:
    """A fresh Server holding two Apps, `a` and `b`, with no Environments yet."""

    def __init__(
        self,
        directory: Path,
        prefix: str,
        n: int,
        *,
        slots: int,
        snapshots: int | None,
        limits: Limits | None,
        disk_used: int,
        settings: str | None,
    ) -> None:
        self.dir = directory
        self.state = directory / "srv"
        self.bin = directory / "bin"
        self.client_bin = directory / "client-bin"
        # What bootstrap installs: the entry command, the deploy script and the template check
        # beside it, never delivered.
        self.sbin = directory / "sbin"
        self.lib = directory / "lib"
        for d in (self.state, self.bin, self.client_bin, self.sbin, self.lib, directory / "home"):
            d.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "python3",
                "-I",
                "-c",
                INSTALL,
                str(REPO / "src"),
                "--lib",
                str(self.lib),
                "--sbin",
                str(self.sbin),
                "--no-chown",
            ],
            check=True,
            capture_output=True,
        )
        for fake, target in (
            ("sudo", self.bin),
            ("tailscale", self.bin),
            ("ssh", self.client_bin),
        ):
            shutil.copyfile(FAKES / fake, target / fake)
            (target / fake).chmod(0o755)
        self.set_disk_used(disk_used)
        if settings is not None:
            (self.state / "settings").write_text(settings)

        # What `deadlift app add` gives each App: its own block of ports, its Slots and its limits.
        short = prefix.removeprefix("deadlift-")
        self.apps: dict[AppKey, App] = {
            "a": App(f"{short}-{n}a", 0),
            "b": App(f"{short}-{n}b", 0),
        }
        for app in self.apps.values():
            app.port_base = free_port_block([other.port_base for other in self.apps.values()])
            conf = self.state / "apps" / app.name
            conf.mkdir(parents=True)
            (conf / "app.conf").write_text(
                f"port_base={app.port_base}\nslots={slots}\n"
                + (limits.lines() if limits else "")
                + (f"snapshots={snapshots}\n" if snapshots else "")
            )

    def set_disk_used(self, percent: int) -> None:
        """Sets how full the fake Server's disk is, in percent, as the deploy script reads it."""
        (self.dir / "server.env").write_text(
            f"DEADLIFT_STATE_DIR={self.state}\nDEADLIFT_LIB_DIR={self.lib}\n"
            f"DEADLIFT_DISK_USED={percent}\n"
        )

    def cleanup(self) -> None:
        """Removes every Docker object of this Server's Apps: their Compose projects are all
        named deadlift-<app>-<environment>."""
        for app in self.apps.values():
            by_name = ["--filter", f"name=^deadlift-{app.name}-"]
            _remove_listed(["ps", "--all", *by_name], ["rm", "--force", "--volumes"])
            _remove_listed(["volume", "ls", *by_name], ["volume", "rm"])
            _remove_listed(["network", "ls", *by_name], ["network", "rm"])
        shutil.rmtree(self.dir, ignore_errors=True)

    def _client_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return {
            "PATH": f"{self.client_bin}:{os.environ.get('PATH', '')}",
            "HOME": os.environ.get("HOME", ""),
            **_docker_host(),
            "DEADLIFT_TEST_SERVER": str(self.dir),
            **(extra or {}),
        }

    def dev_port(self, app: AppKey) -> int:
        """Where the Dev Environment of `app` listens on the host's loopback: the App's base."""
        return self.apps[app].port_base

    def slot_port(self, app: AppKey, slot: int) -> int:
        """Where Slot `slot` of `app` listens on the host's loopback: the App's base + 10 x slot."""
        return self.apps[app].port_base + 10 * slot

    def deliver(
        self,
        app: str,
        args: Sequence[str],
        *,
        template: str | None = None,
        token: str | None = None,
    ) -> Delivery:
        """Runs `deliver` as the App's CI job would, from a checkout holding `template` as its
        compose.yaml (the example App's by default), and returns what it reported."""
        job = Path(tempfile.mkdtemp(prefix="job-", dir=self.dir))
        (job / "compose.yaml").write_text(template if template is not None else example_template())
        output_file = job / "github-output"
        output_file.write_text("")
        summary_file = job / "step-summary"
        summary_file.write_text("")
        # As the deliver action runs it.
        result = collect(
            [
                "python3",
                "-I",
                str(REPO / "actions" / "deliver" / "deliver.py"),
                *("--server", "server.tailtest.ts.net", "--app", app, *args),
            ],
            cwd=job,
            env=self._client_env(
                {
                    "GITHUB_OUTPUT": str(output_file),
                    "GITHUB_STEP_SUMMARY": str(summary_file),
                    "DEADLIFT_REGISTRY_TOKEN": token
                    if token is not None
                    else "test-registry-token",
                }
            ),
        )
        outputs = parse_github_output(output_file.read_text())
        summary = summary_file.read_text()
        shutil.rmtree(job, ignore_errors=True)
        return Delivery(
            code=result.code,
            output=result.output,
            outcome=outputs.get("outcome"),
            reason=outputs.get("reason"),
            url=outputs.get("url"),
            environment=outputs.get("environment"),
            swept=outputs.get("swept"),
            summary=summary,
        )

    def send(self, user: str, args: Sequence[str], input: bytes) -> Result:
        """Calls the entry command over the `ssh` shim as `user`, with `input` on stdin, the way
        a client other than `deliver` could. `args` follow the entry command."""
        command = ["sudo", "--non-interactive", "/usr/local/sbin/deadlift-entry", *args]
        return collect(
            ["ssh", f"{user}@server.tailtest.ts.net", *map(shell_quote, command)],
            env=self._client_env(),
            input=input,
        )

    def secrets(self, app: str, kind: Literal["dev", "feature"], contents: str) -> Path:
        """Puts an App's secrets file for its Dev or Feature Environments on the Server, as
        `deadlift app secrets <app> dev|feature <file>` does. Returns where it is on the Server."""
        file = self.state / "apps" / app / f"{kind}.env"
        file.touch(mode=0o600)
        file.write_text(contents)
        return file

    def serve_mappings(self) -> list[ServeMapping]:
        """What the fake `tailscale` serves now, for every App's Service."""
        completed = subprocess.run(
            [str(self.bin / "tailscale"), "serve", "status", "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        status = cast(dict[str, dict[str, dict[str, object]]], json.loads(completed.stdout))
        mappings: list[ServeMapping] = []
        for service, config in status.get("Services", {}).items():
            web = cast(dict[str, dict[str, dict[str, dict[str, str]]]], config["Web"])
            for host, handlers in web.items():
                mappings.append(
                    ServeMapping(
                        service,
                        int(host[host.rindex(":") + 1 :]),
                        handlers["Handlers"]["/"]["Proxy"],
                    )
                )
        return mappings

    def tailscale_calls(self) -> list[str]:
        """Every call the fake `tailscale` got, one per line."""
        try:
            log = (self.dir / "tailscale" / "calls.log").read_text()
        except FileNotFoundError:
            return []
        return [line for line in log.split("\n") if line]

    def hold_lock(self) -> Callable[[], None]:
        """Holds the Server's lock, as another operation in progress would, until released."""
        lock = str(self.state / "lock")
        holder = subprocess.Popen(["flock", lock, "cat"], stdin=subprocess.PIPE)
        # flock prints nothing, so wait until the lock is visibly taken.
        while subprocess.run(["flock", "--nonblock", lock, "true"], check=False).returncode == 0:
            time.sleep(0.05)

        def release() -> None:
            assert holder.stdin is not None
            holder.stdin.close()
            holder.wait()

        return release

    def watchdog(self) -> Result:
        """Runs the watchdog as the Server's systemd timer does: the installed deploy script, as
        root, in a fresh environment."""
        settings = [line for line in (self.dir / "server.env").read_text().split("\n") if line]
        docker_host = [f"DOCKER_HOST={value}" for value in _docker_host().values()]
        return collect(
            [
                "env",
                "-i",
                f"PATH={self.bin}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                *docker_host,
                *settings,
                str(self.lib / "deploy.sh"),
                "watchdog",
            ],
            env={},
        )

    def has_image(self, ref: str) -> bool:
        """Whether the Server's Docker holds the image `ref`, as `docker image ls` there would."""
        return _docker_ok("image", "inspect", ref)

    def containers(self, app: str) -> str:
        """The names of one App's Docker containers, running or not."""
        return docker("ps", "--all", "--format", "{{.Names}}", "--filter", f"name=^deadlift-{app}-")

    def running_containers(self, app: str) -> str:
        """The names of one App's running Docker containers."""
        return docker("ps", "--format", "{{.Names}}", "--filter", f"name=^deadlift-{app}-")

    def started_at(self, app: str, environment: str, service: str) -> str:
        """When a container of one App's Environment last started, to tell whether it was
        touched."""
        return docker(
            "inspect",
            "--format",
            "{{.State.StartedAt}}",
            f"deadlift-{app}-{environment}-{service}-1",
        )

    def _snapshots_dir(self, app: str) -> Path:
        return self.state / "apps" / app / "environments" / "dev" / "snapshots"

    def volume_snapshots(self, app: str) -> list[str]:
        """The Volume Snapshots the Server keeps of one App's Dev Environment, as an operator
        listing the Server's state would see them. Nothing else shows them yet."""
        try:
            return sorted(path.name for path in self._snapshots_dir(app).iterdir())
        except FileNotFoundError:
            return []

    def file_in_volume_snapshot(self, app: str, snapshot: str, path: str) -> str:
        """A file as one of `app`'s Volume Snapshots holds it, at `path` in one of Dev's named
        volumes, as an operator unpacking it would read it. Empty when no volume in it has one."""
        directory = self._snapshots_dir(app) / snapshot
        for tar in sorted(directory.iterdir()):
            if not tar.name.endswith(".tar"):
                continue
            unpacked = subprocess.run(
                ["tar", "-xOf", str(tar), f"./{path}"], capture_output=True, check=False
            )
            if unpacked.returncode == 0:
                return unpacked.stdout.decode()
        return ""


@dataclass
class Harness:
    """One test run: a local registry and the example App's images in it. Call `remove` when
    done."""

    prefix: str
    root: Path
    repository: str
    _images: dict[str, str] = field(default_factory=dict[str, str])
    _servers: list[Server] = field(default_factory=list[Server])
    _server_count: int = 0

    def _build(self, kind: ImageKind, version: int) -> str:
        """Builds an image and pushes it, leaving no local copy, so a deploy must pull it."""
        ref = f"{self.repository}:v{version}-{kind}"
        docker(
            "build",
            "--quiet",
            "--build-arg",
            f"VERSION={version}",
            "--build-arg",
            f"HEALTH={'fail' if kind == 'unhealthy' else 'ok'}",
            "--build-arg",
            f"MIGRATE={'fail' if kind == 'failing-migration' else 'ok'}",
            "--tag",
            ref,
            str(EXAMPLE_APP),
        )
        # Retried: a fresh registry can refuse the first push for a moment.
        for attempt in range(1, 6):
            try:
                docker("push", "--quiet", ref)
                break
            except RuntimeError:
                if attempt == 5:
                    raise
                time.sleep(1)
        docker("image", "rm", "--force", ref)
        return ref

    def image(self, kind: ImageKind = "healthy", version: int = 1) -> str:
        """A reference to one of the example App's images, built and pushed on first use."""
        key = f"{kind}-{version}"
        if key not in self._images:
            self._images[key] = self._build(kind, version)
        return self._images[key]

    def server(
        self,
        *,
        slots: int = 3,
        snapshots: int | None = None,
        limits: Limits | None = None,
        disk_used: int = 50,
        settings: str | None = None,
    ) -> Server:
        """A fresh Server holding two Apps, `a` and `b`, with no Environments yet. Each App has
        `slots` Slots and, when given, the number of Volume Snapshots to keep and the
        per-container `limits` `deadlift app add` sets. Its disk is `disk_used` percent full, in
        place of measuring this machine's, which may be fuller than a Server's thresholds allow.
        `settings` is the Server-wide settings file as bootstrap writes it; without it there's
        none."""
        self._server_count += 1
        n = self._server_count
        server = Server(
            self.root / f"server-{n}",
            self.prefix,
            n,
            slots=slots,
            snapshots=snapshots,
            limits=limits,
            disk_used=disk_used,
            settings=settings,
        )
        self._servers.append(server)
        return server

    def remove_servers(self) -> None:
        """Removes every Server made so far."""
        servers, self._servers = self._servers, []
        for server in servers:
            server.cleanup()

    def remove(self) -> None:
        """Removes everything this run made."""
        self.remove_servers()
        _docker_ok("rm", "--force", "--volumes", f"{self.prefix}-registry")
        refs = docker("image", "ls", "--format", "{{.Repository}}:{{.Tag}}", self.repository)
        if refs:
            _docker_ok("image", "rm", "--force", *refs.split("\n"))
        shutil.rmtree(self.root, ignore_errors=True)


def start_harness() -> Harness:
    """Starts one test run: a local registry, for the example App's images."""
    require_tools()
    prefix = f"deadlift-test-{secrets.token_hex(4)}"
    root = Path(tempfile.mkdtemp(prefix=f"{prefix}-"))
    # The registry shares the daemon's network and listens on its loopback, which Docker trusts
    # without TLS. Its debug server would take :5001, so a second run couldn't start. On a
    # bootstrapped Server, Docker remaps users, and shares the host's network only with a
    # container that opts out of the remap.
    registry_port = free_port()
    registry_debug_port = free_port()
    docker(
        "run",
        "--detach",
        "--name",
        f"{prefix}-registry",
        "--userns",
        "host",
        "--network",
        "host",
        "--env",
        f"REGISTRY_HTTP_ADDR=127.0.0.1:{registry_port}",
        "--env",
        f"REGISTRY_HTTP_DEBUG_ADDR=127.0.0.1:{registry_debug_port}",
        REGISTRY_IMAGE,
    )
    return Harness(prefix, root, f"127.0.0.1:{registry_port}/{prefix}/example")


def _remove_listed(listing: Sequence[str], remove: Sequence[str]) -> None:
    """Removes what Docker lists for one filter, if anything."""
    ids = docker(*listing, "--quiet")
    if ids:
        docker(*remove, *ids.split("\n"))


@dataclass(frozen=True)
class Symlink:
    """A member of a hand-made delivery that is a symlink to `target`."""

    target: str


@dataclass(frozen=True)
class Directory:
    """A member of a hand-made delivery that is a directory."""


def tar_of(members: Mapping[str, str | Symlink | Directory]) -> bytes:
    """A tar archive holding `members` (a file's contents, a `Symlink` or a `Directory`), in that
    order, as a client other than `deliver` could send."""
    with tempfile.TemporaryDirectory(prefix="deadlift-test-tar-") as d:
        directory = Path(d)
        for name, member in members.items():
            match member:
                case str():
                    (directory / name).write_text(member)
                case Symlink(target):
                    (directory / name).symlink_to(target)
                case Directory():
                    (directory / name).mkdir()
        return subprocess.run(
            ["tar", "-C", d, "-cf", "-", *members], capture_output=True, check=True
        ).stdout


def put_note(port: int, note: str) -> None:
    """Writes a note through the example App on the host's loopback, into its named volume."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/note", data=note.encode(), method="PUT"
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310
        response.read()


def get(port: int, path: str) -> str:
    """Fetches a path from an Environment on the host's loopback."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as response:
            return cast(bytes, response.read()).decode()
    except urllib.error.HTTPError as error:
        return error.read().decode()
