"""deadlift's deploy script: runs one operation on one App's Environments. Bootstrap installs it on
the Server, owned by root, at /usr/local/lib/deadlift/deploy, and it's never delivered
(ADR 0001): a fix to it reaches a Server when `deadlift server bootstrap` is re-run. Only the
entry command runs it, as root, after working out the App from the calling user and unpacking
the delivery.

Usage:
  deploy --app <app> --delivery <dir> deploy dev|feature-<key> [--image <name>=<ref>]...
      [--keep <keys> | --keep-none]
  deploy --app <app> --delivery <dir> rollback dev [--discard-newer-data]
  deploy --app <app> --delivery <dir> destroy feature-<key>
  deploy --app <app> --delivery <dir> sweep --keep <keys> | --keep-none
  deploy watchdog

  --app      The App, as the entry command worked it out. Never from a delivery's arguments.
  --delivery  The unpacked delivery: compose.yaml, the App's Compose template, and token, the
              job's registry token.

Operations:
  deploy <environment> --image <name>=<ref>... [--keep <keys> | --keep-none]
      Deploys the delivered template to the Environment, with each image reaching the template
      as ${DEADLIFT_IMAGE_<NAME>}. A Feature Environment, feature-<key>, takes a free Slot of
      the App's, or the one its key already holds, and is skipped when none is free. A
      redeploy keeps the Environment's Slot, URL and volumes, and a failed one leaves it
      stopped. A Feature deploy is skipped when the disk is over the Server's lower threshold,
      so what's left goes to the Dev Environments. With --keep or --keep-none, a Feature deploy
      first sweeps the App as `sweep` does, always keeping the Environment it deploys, so Slots
      held by keys no longer kept are free for it. It sweeps only once the template is checked
      and the images pulled, and not at all when it's skipped.

      Every image the template names is pulled with the delivery's token, even one the Server
      has, so an App runs another App's image only if the registry gives it to its own CI too.
      The Dev Environment, dev, is checked and pulled before anything stops, so a bad template
      or image reference leaves it running as it was. Then Dev stops, a Volume Snapshot is taken
      of every named volume, and the new version starts. When it doesn't, Recovery brings Dev
      down, puts the snapshot back and starts the previous version, and the reason says whether
      that worked. The new version is recorded as current, and the one before it as previous,
      for Rollback.

      After every deploy, whatever its outcome, the App's images that no Environment's version
      records, of any App, and no container uses are removed from the Server.

  rollback dev [--discard-newer-data]
      Takes the Dev Environment back to its previous version, which becomes its current
      version, and is then refused until Dev has been deployed again: one step only. Only Dev
      has versions, so a Feature Environment is refused. The previous version's images are
      pulled, if the Server no longer has them, before anything stops. Then Dev stops and the
      previous version starts on today's data, and is kept when it's healthy. When it isn't,
      the current version starts again and Rollback is refused, unless --discard-newer-data is
      given: then a Volume Snapshot of today's data is taken, the Volume Snapshot from before
      the current version was first deployed is put back, and the previous version starts on
      it. If it still doesn't start, today's data and the current version are put back.

  destroy feature-<key>
      Removes the Feature Environment, its volumes and its Service mapping, and frees its
      Slot. Done, changing nothing, when the App has no such Environment.

  sweep --keep <keys> | --keep-none
      The Sweep: destroys every Feature Environment of the App whose key isn't in <keys>,
      separated by commas, or every one with --keep-none, and outputs the swept keys. An empty
      <keys> is refused, so a list that failed to come never sweeps everything. It goes on past
      an Environment it can't destroy, then fails naming it. The Dev Environment, and other
      Apps' Environments, are never swept.

  watchdog
      Run by the Server's systemd timer, which bootstrap installs, and never by a delivery: it
      acts on every App. When the disk is over the Server's upper threshold, it stops every
      Feature Environment on the Server, keeping their data, state, Slots and Serve mappings,
      so each starts again on its next deploy. Dev Environments are never touched.

Disk thresholds are the Server-wide settings, in /srv/deadlift/settings (see bootstrap):
feature_deploy_disk_limit (80) and watchdog_disk_limit (90), each a percentage of the disk
used, measured on the fuller of the filesystems holding the Server's state and Docker's data.

A Feature Environment's key is up to 30 of a-z, 0-9 and -, starting with a letter or digit.

The template must name the one service deadlift routes to as `x-deadlift.route:
<service>:<port>`, and that service must have a Compose healthcheck that isn't disabled
(ADR 0002). Anything ADR 0001 refuses is refused before anything stops: the template check
(template.py) checks the template and writes deadlift's override, which publishes the routed
service on the host's loopback at the Environment's port and sets the App's limits.
deadlift then starts the Environment with `docker compose up`, and waits for the routed service
to be healthy. It waits on that service only, because `up --wait` over a whole project fails
when a one-shot service that nothing depends on exits 0. A migration the routed service depends
on still has to succeed first.

Variables reach the template through Compose's ${VAR} substitution, and nothing else: the
App's secrets file for the kind of Environment (dev.env for Dev, feature.env for Feature),
and deadlift's own, which win over the secrets file's:
  DEADLIFT_ENV             The Environment: dev or feature-<key>.
  DEADLIFT_APP             The App.
  DEADLIFT_URL             The Environment's URL.
  DEADLIFT_SECRET          Random, made on the Environment's first deploy and kept until it's
                           destroyed.
  DEADLIFT_IMAGE_<NAME>    Each --image.

Ports: each App has a block of 100 loopback ports from its `port_base`. The Dev Environment is
at the base, and the Feature Environment holding Slot s at base + 10s. Tailscale Serve maps
them on the App's own Service, svc:<app> (ADR 0003): Dev at HTTPS 443, and Slot s at
10000 + s, so a Feature Environment's URL stays the same while it holds its Slot.

State, all root-only, under /srv/deadlift:
  lock                            The Server's lock: one operation at a time.
  settings                        The Server-wide settings, `name=value` lines, by bootstrap.
  apps/<app>/app.conf             The App's entry in the Server registry, written by
                                  `deadlift app add`: port_base=<port>, slots=<n>,
                                  snapshots=<n>, the Volume Snapshots to keep (3 if unset),
                                  and the per-container limits memory=<size, e.g. 512m>,
                                  cpus=<n> and pids=<n> (template.py has the defaults).
  apps/<app>/dev.env, feature.env The App's secrets files, written by `deadlift app secrets`.
  apps/<app>/environments/<env>/  An Environment: its template (compose.yaml), the variables
                                  it was deployed with (secrets.env, a copy of its secrets
                                  file, and vars.env, deadlift's), its DEADLIFT_SECRET (secret),
                                  deadlift's override (override.json) and, for Feature, the
                                  Slot it holds (slot). Destroying it removes the directory.
    dev/                          For Dev, these are its current version, which also has its
                                  image references (images: <name>=<ref> per line) and the
                                  name of the Volume Snapshot taken just before it was first
                                  deployed (snapshot), which --discard-newer-data goes back to.
    dev/previous/                 The version before the current one, in the same files, which
                                  Rollback starts. Absent until Dev has had two versions.
    dev/snapshots/<name>/         A Volume Snapshot: <volume>.tar for each of Dev's named
                                  volumes, taken with its containers stopped. The newest are
                                  kept, and the one the current version records.
  deliveries/                     Deliveries while their operation runs.

Every reason line starts with `deadlift: `, and the last one says what happened. Outputs for
`deliver` are lines of their own: `deadlift-output: <name>=<value>`.

Exit codes: 0 done, 1 failed, 2 refused or bad usage (nothing changed), 75 skipped.
"""

import base64
import contextlib
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NoReturn, cast

from deadlift.core.lock import server_lock
from deadlift.core.outcome import Exit, Stop, output, reason
from deadlift.core.process import Env, Stream, Target, run, scrubbed_env, start
from deadlift.server import template
from deadlift.server.text import jq_text, last_line, lines, read_settings, sentence

USAGE = (
    "Usage: deploy --app <app> --delivery <dir> deploy dev|feature-<key> "
    "[--image <name>=<ref>]... [--keep <keys> | --keep-none]\n"
    "Usage: deploy --app <app> --delivery <dir> rollback dev [--discard-newer-data]\n"
    "Usage: deploy --app <app> --delivery <dir> destroy feature-<key>\n"
    "Usage: deploy --app <app> --delivery <dir> sweep --keep <keys> | --keep-none\n"
    "Usage: deploy watchdog\n"
)

# How long the routed service has to report healthy.
HEALTH_TIMEOUT = "300"
# Takes Volume Snapshots and puts them back: only its tar, sh and find are used.
SNAPSHOT_IMAGE = (
    "alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc"
)
# The files that make up a version of the Dev Environment, in its directory: the template
# (compose.yaml), the variables and deadlift's override it runs with (secrets.env, vars.env,
# override.json), its image references (images, <name>=<ref> per line), and the name of the
# Volume Snapshot taken just before it was first deployed (snapshot). DEADLIFT_SECRET (secret)
# belongs to the Environment, not to a version.
VERSION_FILES = ("compose.yaml", "secrets.env", "vars.env", "override.json", "images", "snapshot")

_PERCENT = re.compile(r"100|[1-9]?[0-9]")
_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,29}")
_APP = re.compile(r"[a-z0-9][a-z0-9-]*")
_IMAGE = re.compile(r"([a-z][a-z0-9_]*)=([a-zA-Z0-9][a-zA-Z0-9._/:@-]*)")


class _Said(Exception):
    """Ends the script with ``code``, its reason already given."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def usage(why: str) -> NoReturn:
    reason(why)
    sys.stderr.write(USAGE)
    sys.stderr.flush()
    raise Stop(Exit.REFUSED, None)


def say(text: str) -> None:
    """A progress line, on stdout."""
    sys.stdout.write(f"{text}\n")
    sys.stdout.flush()


def tell(text: str) -> None:
    """A line on stderr, such as a reason that isn't the last."""
    sys.stdout.flush()
    sys.stderr.write(f"{text}\n")
    sys.stderr.flush()


def repository_of(ref: str) -> str:
    """The repository of the image reference ``ref``: without its digest or tag."""
    ref = ref.partition("@")[0]
    if ":" in ref.rpartition("/")[2]:
        ref = ref.rpartition(":")[0]
    return ref


def keep_keys(value: str) -> list[str]:
    """--keep's keys, as Bash's ``IFS=, read -ra keys <<<"$1"`` read them: from its first line,
    split at commas, less one empty field after a trailing comma."""
    keys = value.partition("\n")[0].split(",")
    if keys[-1] == "":
        keys.pop()
    return keys


def _sort_n_key(text: str) -> tuple[float, str]:
    matched = re.match(r"\s*(-?[0-9]*\.?[0-9]+|-?[0-9]+\.?)", text)
    return (float(matched[1]) if matched else 0.0, text)


def fullest_disk(df: str) -> str:
    """The highest percentage used in ``df -P``'s output, as
    ``awk 'NR > 1 { sub(/%$/, "", $5); print $5 }' | sort -n | tail -n 1`` gives it."""
    values = [
        fields[4].removesuffix("%") if len(fields) >= 5 else ""
        for fields in (line.split() for line in lines(df)[1:])
    ]
    return max(values, key=_sort_n_key) if values else ""


def _read_text(path: Path) -> str:
    """A file as ``$(<file)`` reads it: without its trailing line breaks."""
    return path.read_bytes().decode("utf-8", errors="surrogateescape").rstrip("\n")


def _copy(source: Path, target: Path) -> None:
    """``cp source target``: the contents, the mode this process's umask gives."""
    shutil.copyfile(source, target)


def _same(one: Path, other: Path) -> bool:
    """``cmp -s``: both are there and hold the same bytes."""
    try:
        return one.read_bytes() == other.read_bytes()
    except OSError:
        return False


def _remove_tree(path: Path) -> None:
    """``rm -rf``."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


class Server:
    """What every operation knows: the Server's state, and the environment its children get."""

    def __init__(self, state: Path) -> None:
        self.state = state
        # What every child gets: PATH, HOME and DOCKER_HOST as this process has them, nothing
        # else. `docker compose` gets HOME=/root when there's none, as the Bash gave it.
        self.env: Env = scrubbed_env(keep=("PATH", "HOME", "DOCKER_HOST"))
        self.compose_env: Env = template.child_env()
        # Set once a deploy holds the Server's lock: its App's images are to be pruned.
        self.prune_pending = False

    def docker(
        self,
        *args: str,
        stdout: Target = Stream.STDERR,
        stderr: Target = Stream.INHERIT,
        stdin: Target = Stream.DEVNULL,
        env: Env | None = None,
    ) -> "tuple[bool, str]":
        done = run(
            ["docker", *args],
            env=self.env if env is None else env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
        return done.ok, done.stdout

    def read_settings(self) -> tuple[int, int]:
        """The Server-wide settings: feature_deploy_disk_limit and watchdog_disk_limit. One
        missing, or the whole file, takes its default, as bootstrap would set it."""
        path = self.state / "settings"
        feature, watchdog = "80", "90"
        if path.is_file():
            for key, value in read_settings(path):
                if key == "feature_deploy_disk_limit":
                    feature = value
                elif key == "watchdog_disk_limit":
                    watchdog = value
        if not (_PERCENT.fullmatch(feature) and _PERCENT.fullmatch(watchdog)):
            self.fail(
                f"the Server-wide settings in {path} are broken: each disk limit must be a whole "
                "percentage, 0 to 100. Fix the file, so nothing was changed"
            )
        return int(feature), int(watchdog)

    def disk_used(self) -> int | None:
        """How full the disk is, as a whole percentage: the fuller of the filesystems holding
        the Server's state and Docker's data. DEADLIFT_DISK_USED replaces the measure, for the
        tests. None when it can't be measured."""
        override = os.environ.get("DEADLIFT_DISK_USED", "")
        if override:
            return int(override)
        paths = [str(self.state)]
        _, root = self.docker(
            "info", "--format", "{{.DockerRootDir}}", stdout=Stream.CAPTURE, stderr=Stream.DEVNULL
        )
        root = root.rstrip("\n")
        if root and Path(root).is_dir():
            paths.append(root)
        # POSIX output: the fifth column is the percentage used.
        df = run(["df", "-P", *paths], env=self.env, stdout=Stream.CAPTURE)
        used = fullest_disk(df.stdout)
        return int(used) if re.fullmatch(r"[0-9]+", used) else None

    # Ways out. Each prunes first when a deploy has its images to prune, so the reason stays
    # the last line.

    def refuse(self, why: str) -> NoReturn:
        self.prune_once()
        raise Stop(Exit.REFUSED, why)

    def fail(self, why: str) -> NoReturn:
        self.prune_once()
        raise Stop(Exit.FAILED, why)

    def skip(self, why: str) -> NoReturn:
        self.prune_once()
        raise Stop(Exit.SKIPPED, why)

    def prune_once(self) -> None:
        """Overridden by a deploy, which prunes its App's images once it has an outcome."""

    def prune_quietly(self) -> None:
        """Overridden by a deploy."""

    def locked(self, body: Callable[[], None]) -> None:
        """Runs ``body`` holding the Server's lock, and gives its reason before letting go. A
        deploy's way out that didn't prune, such as a template refused, prunes without a word."""
        with server_lock(self.state):
            try:
                body()
            except Stop as stop:
                if stop.why is not None:
                    reason(stop.why)
                raise _Said(int(stop.code)) from None
            finally:
                self.prune_quietly()


class App(Server):
    """An operation on one App's Environments, for a delivery."""

    def __init__(self, state: Path, app: str, delivery: Path) -> None:
        super().__init__(state)
        self.app = app
        self.delivery = delivery
        self.env_name = ""
        self.project = ""
        # The --image arguments: each image's reference, by name.
        self.images: dict[str, str] = {}
        # --keep or --keep-none: whether either was given, and the Feature Environments to keep.
        self.keep_given = False
        self.keep: list[str] = []
        # What a Sweep destroyed, and what it couldn't.
        self.swept: list[str] = []
        self.unswept: list[str] = []
        # The last start's output (up.log in the Bash), whose last line is the reason.
        self.up_log = ""
        self.port_base = 0
        self.slots = 0
        self.snapshots_kept = 3
        self.snapshot = ""
        self.config: dict[str, object] = {}
        self.routed = ""
        self.discard = False

    @property
    def environments(self) -> Path:
        return self.state / "apps" / self.app / "environments"

    def check_feature_key(self) -> None:
        """Refuses the Environment unless its key is up to 30 of a-z, 0-9 and -, so it's safe
        in paths, Compose project names and URLs."""
        key = self.env_name.removeprefix("feature-")
        if not _KEY.fullmatch(key):
            usage(f"'{key}' isn't a Feature Environment key: up to 30 of a-z, 0-9 and -")

    def read_app(self) -> None:
        """Reads the App's entry in the Server registry: port_base, slots and snapshots."""
        conf = self.state / "apps" / self.app / "app.conf"
        port_base, slots, snapshots = "", "", "3"
        if not conf.is_file():
            self.refuse(f"{self.app} isn't an App on this Server")
        for key, value in read_settings(conf):
            if key == "port_base":
                port_base = value
            elif key == "slots":
                slots = value
            elif key == "snapshots":
                snapshots = value
        if not (
            re.fullmatch(r"[0-9]+", port_base)
            and re.fullmatch(r"[1-9]", slots)
            and re.fullmatch(r"[1-9][0-9]*", snapshots)
        ):
            self.fail(
                f"{self.app}'s entry in the Server registry is broken. Re-run "
                f"`deadlift app add {self.app}`"
            )
        self.port_base, self.slots, self.snapshots_kept = int(port_base), int(slots), int(snapshots)

    def read_tailnet_suffix(self) -> str:
        """The tailnet's MagicDNS suffix, for the App's address on its Service."""
        done = run(["tailscale", "status", "--json"], env=self.env, stdout=Stream.CAPTURE)
        suffix = ""
        if done.ok:
            try:
                status: object = json.loads(done.stdout)
            except ValueError:
                status = None
            if isinstance(status, dict):
                value = cast(dict[str, object], status).get("MagicDNSSuffix")
                if value is not None and value is not False:
                    suffix = jq_text(value).rstrip("\n")
        if not suffix:
            self.fail("couldn't read the tailnet's name from Tailscale, so nothing was changed")
        return suffix

    def compose_argv(self, directory: Path, *args: str) -> list[str]:
        """`docker compose` for the Environment whose files are in ``directory``, as the project
        :attr:`project`: its template, and deadlift's override once there is one. The template
        sees only the variables in secrets.env and vars.env, vars.env winning: nothing from this
        process's own environment, or its caller's, reaches it."""
        files = ["--file", str(directory / "compose.yaml")]
        if (directory / "override.json").is_file():
            files += ["--file", str(directory / "override.json")]
        if (directory / "secrets.env").is_file():
            files = ["--env-file", str(directory / "secrets.env"), *files]
        return [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--project-directory",
            str(directory),
            *files,
            "--env-file",
            str(directory / "vars.env"),
            *args,
        ]

    def compose(
        self,
        directory: Path,
        *args: str,
        stdout: Target = Stream.STDERR,
        stderr: Target = Stream.INHERIT,
    ) -> "tuple[bool, str]":
        done = run(
            self.compose_argv(directory, *args), env=self.compose_env, stdout=stdout, stderr=stderr
        )
        return done.ok, done.stdout

    def compose_teed(self, directory: Path, *args: str) -> bool:
        """``compose ... 2>&1 | tee up.log >&2``: its output on stderr as it comes, and kept in
        :attr:`up_log`."""
        read_end, write_end = os.pipe()
        try:
            child = start(
                self.compose_argv(directory, *args),
                env=self.compose_env,
                stdout=write_end,
                stderr=write_end,
            )
        except OSError as error:
            os.close(read_end)
            os.close(write_end)
            self.up_log = f"{error}\n"
            tell(str(error))
            return False
        os.close(write_end)
        sys.stdout.flush()
        sys.stderr.flush()
        log = bytearray()
        with os.fdopen(read_end, "rb", buffering=0) as pipe:
            while chunk := pipe.read(65536):
                log += chunk
                sys.stderr.buffer.write(chunk)
                sys.stderr.buffer.flush()
        code = child.wait()
        self.up_log = log.decode("utf-8", errors="replace")
        return code == 0

    def write_variables(self, directory: Path, url: str) -> None:
        """Writes the variables the template in ``directory`` will see, for this Environment at
        ``url``: secrets.env, a copy of the App's secrets file for that kind of Environment
        (dev.env or feature.env), and vars.env, deadlift's own variables, which win over the
        secrets file's. Also writes secret, the Environment's DEADLIFT_SECRET: the one it
        already has, or a new random one on its first deploy, so it's kept until the Environment
        is destroyed."""
        kind = self.env_name.partition("-")[0]
        secrets = self.state / "apps" / self.app / f"{kind}.env"
        secret = self.environments / self.env_name / "secret"
        if secrets.is_file():
            _copy(secrets, directory / "secrets.env")
        else:
            (directory / "secrets.env").write_bytes(b"")
        # Read on its own first, so a broken secrets file isn't blamed on the template.
        done = run(
            [
                "docker",
                "compose",
                "--project-name",
                self.project,
                "--project-directory",
                str(directory),
                "--env-file",
                str(directory / "secrets.env"),
                "--file",
                "-",
                "config",
                "--quiet",
            ],
            env=self.compose_env,
            input="services: {}\n",
            stdout=Stream.CAPTURE,
            stderr=Stream.CAPTURE,
        )
        if not done.ok:
            error = (done.stdout + done.stderr).rstrip("\n")
            marker = "secrets.env: "
            if marker in error:
                error = error[error.rindex(marker) + len(marker) :]
            self.fail(
                f"{self.app}'s {secrets.name} on this Server isn't a valid env file: {error}. Fix "
                f"it and re-run `deadlift app secrets {self.app} {kind}`. Nothing was changed"
            )
        if secret.is_file() and secret.stat().st_size > 0:
            _copy(secret, directory / "secret")
        else:
            (directory / "secret").write_text(os.urandom(32).hex())
        variables = [
            f"DEADLIFT_ENV={self.env_name}",
            f"DEADLIFT_APP={self.app}",
            f"DEADLIFT_URL={url}",
            f"DEADLIFT_SECRET={_read_text(directory / 'secret')}",
            *(f"DEADLIFT_IMAGE_{name.upper()}={ref}" for name, ref in self.images.items()),
        ]
        (directory / "vars.env").write_text("".join(f"{line}\n" for line in variables))

    def check_template(self, directory: Path) -> None:
        """Checks the template in ``directory``, keeping the service its x-deadlift.route names
        and the template as Compose normalises it. Refuses, changing nothing, a template
        deadlift won't run (ADR 0001, ADR 0002)."""
        checked = template.check(directory, self.project)
        self.config = checked.config
        route = jq_text(cast(dict[str, object], checked.config["x-deadlift"])["route"])
        self.routed = route.rpartition(":")[0] if ":" in route else route

    def write_override(self, directory: Path, port: int) -> None:
        """Writes deadlift's override for the checked template in ``directory``: the routed
        service published on the host's loopback at ``port``, and the App's limits and
        deadlift's hardening on every service."""
        document = template.override(
            directory,
            self.project,
            port=str(port),
            app=self.app,
            environment=self.env_name,
            limits=self.state / "apps" / self.app / "app.conf",
        )
        (directory / "override.json").write_text(document)

    def pull_images(self, *, again: bool) -> None:
        """Pulls the images the template names, with the job's registry token. Fails, changing
        nothing, when one can't be pulled.

        With ``again``, as for a deploy, every image is pulled, even one the Server has: an
        image another App's deploy left on the Server, such as its private one, runs only if
        the registry gives it to this delivery's token too (ADR 0001). Pulling one the Server has
        only checks it with the registry. Without, as for Rollback, only those the Server no
        longer has are pulled: the previous version's images were pulled when it was deployed.
        """
        docker_config = self.delivery / "docker"
        docker_config.mkdir(parents=True, exist_ok=True)
        # In a config of its own, inside the delivery, so it's removed with it. GHCR checks only
        # the token, so the username is a placeholder.
        token = self.delivery / "token"
        if token.is_file() and token.stat().st_size > 0:
            auth = base64.b64encode(
                f"deadlift:{_read_text(token)}".encode(errors="surrogateescape")
            )
            config = {"auths": {"ghcr.io": {"auth": auth.decode()}}}
            (docker_config / "config.json").write_text(json.dumps(config, indent=2) + "\n")
        refs: set[str] = set()
        services = self.config.get("services")
        if isinstance(services, dict):
            for service in cast(dict[str, object], services).values():
                image = cast(dict[str, object], service).get("image")
                if image is not None and image is not False:
                    refs.add(jq_text(image))
        pull_env = self.env.with_vars({"DOCKER_CONFIG": str(docker_config)})
        for ref in sorted(refs):
            if (
                not again
                and self.docker(
                    "image", "inspect", ref, stdout=Stream.DEVNULL, stderr=Stream.DEVNULL
                )[0]
            ):
                continue
            say(f"Pulling {ref}")
            done = run(
                ["docker", "pull", "--quiet", ref],
                env=pull_env,
                stdout=Stream.DEVNULL,
                stderr=Stream.CAPTURE,
            )
            if not done.ok:
                self.fail(f"couldn't pull {ref}: {last_line(done.stderr)}. Nothing was changed")

    # Slots.

    def held_slots(self) -> list[tuple[str, str]]:
        """Which Slot each of the App's Feature Environments holds, as (Slot, Environment), in
        Slot order."""
        held: list[str] = []
        for slot in sorted(self.environments.glob("feature-*/slot"), key=str):
            if slot.is_file():
                held.append(f"{_read_text(slot)} {slot.parent.name}")
        held.sort(key=_sort_n_key)
        return [cast(tuple[str, str], tuple(line.partition(" ")[::2])) for line in held]

    def holder_of(self, slot: str) -> str:
        """``sed -n "s/^$slot //p"`` over :meth:`held_slots`."""
        return "\n".join(env for held, env in self.held_slots() if held == slot)

    def claim_slot(self) -> int:
        """The Slot this Environment holds, or the lowest free one. With --keep or --keep-none,
        a Slot held by a Feature Environment that isn't kept counts as free, since the Sweep
        before the deploy frees it. Skips when none is free, having changed nothing."""
        mine = self.environments / self.env_name / "slot"
        if mine.is_file():
            return int(_read_text(mine))
        held = self.held_slots()
        for s in range(1, self.slots + 1):
            holder = "\n".join(env for slot, env in held if slot == str(s))
            if not holder or (self.keep_given and holder not in self.keep):
                return s
        holders = [env for _, env in held]
        return self.skip(
            f"all {self.slots} of {self.app}'s Slots are held, by {sentence(holders)}, so "
            f"{self.env_name} wasn't deployed. Destroy one of them to free its Slot"
        )

    def check_disk_for_feature(self) -> None:
        """Skips the Feature deploy when the disk is over the Server's lower threshold, so the
        disk left goes to the Dev Environments."""
        limit, _ = self.read_settings()
        used = self.disk_used()
        if used is None:
            self.fail(
                f"couldn't measure how full the disk is, so {self.env_name} wasn't deployed, and "
                "it was left as it was"
            )
        if used > limit:
            self.skip(
                f"the disk is {used}% full, over the Server's {limit}% limit for Feature "
                f"deploys, so {self.env_name} wasn't deployed, and it was left as it was. Dev "
                "deploys still go ahead"
            )

    def swept_clause(self, before: str) -> str:
        """``${swept[*]:+<before><the swept>}``."""
        return f"{before}{sentence(self.swept)}" if "".join(self.swept) else ""

    def deploy_feature(self) -> None:
        nxt = self.delivery / "next"
        directory = self.environments / self.env_name
        if not (self.delivery / "compose.yaml").is_file():
            self.refuse("the delivery has no compose.yaml to deploy")
        self.project = f"deadlift-{self.app}-{self.env_name}"
        self.check_disk_for_feature()

        nxt.mkdir()
        _copy(self.delivery / "compose.yaml", nxt / "compose.yaml")
        # The Slot comes first, because the template sees the URL it gives.
        suffix = self.read_tailnet_suffix()
        slot = self.claim_slot()
        port = self.port_base + 10 * slot
        public = 10000 + slot
        url = f"https://{self.app}.{suffix}:{public}/"
        self.write_variables(nxt, url)
        self.check_template(nxt)
        self.write_override(nxt, port)
        self.pull_images(again=True)
        # Only now, so a template refused or an image that can't be pulled sweeps nothing.
        if self.keep_given:
            self.sweep_before_deploy(slot)

        directory.mkdir(parents=True, exist_ok=True)
        for name in ("compose.yaml", "secrets.env", "vars.env", "secret", "override.json"):
            _copy(nxt / name, directory / name)
        (directory / "slot").write_text(f"{slot}\n")
        say(f"Starting {self.env_name}")
        if not self.start_environment(directory, self.routed):
            self.compose(directory, "stop", stdout=Stream.DEVNULL, stderr=Stream.DEVNULL)
            self.fail(
                f"{self.env_name} didn't start: {last_line(self.up_log)}. It's stopped, and the "
                f"App's other Environments are as they were{self.swept_clause(' after sweeping ')}"
            )
        if not self.serve(public, port):
            self.compose(directory, "stop", stdout=Stream.DEVNULL, stderr=Stream.DEVNULL)
            self.fail(
                f"{self.env_name} started, but Tailscale Serve couldn't map it at {url}, so it's "
                "stopped again"
            )

        output("environment", self.env_name)
        output("url", url)
        self.prune_once()
        tell(
            f"deadlift: deployed {self.env_name}, which is up at {url}"
            f"{self.swept_clause(', and swept ')}"
        )

    def serve(self, public: int, port: int) -> bool:
        return run(
            [
                "tailscale",
                "serve",
                f"--service=svc:{self.app}",
                f"--https={public}",
                "--bg",
                f"http://127.0.0.1:{port}",
            ],
            env=self.env,
        ).ok

    def sweep_before_deploy(self, slot: int) -> None:
        """For a Feature deploy with --keep or --keep-none: sweeps the App, this Environment
        always kept. Fails if a destroy that failed left the Slot :meth:`claim_slot` picked
        held."""
        self.sweep_features()
        if not self.unswept:
            return
        holder = self.holder_of(str(slot))
        if holder and holder != self.env_name:
            self.fail(
                f"couldn't destroy {holder} (above) to free Slot {slot}, so {self.env_name} "
                f"wasn't deployed{self.swept_clause('. Swept ')}"
            )
        tell(
            f"deadlift: couldn't destroy {sentence(self.unswept)} (above), which the next Sweep "
            "tries again"
        )

    # Destroy and the Sweep.

    def unmap_slot(self, slot: str) -> bool:
        """Removes the Slot's public port from the App's Service, if it's mapped. A Feature
        Environment whose first deploy failed was never mapped."""
        public = str(10000 + int(slot))
        service = f"svc:{self.app}"
        off = run(
            ["tailscale", "serve", f"--service={service}", f"--https={public}", "off"],
            env=self.env,
            stdout=Stream.STDERR,
            stderr=Stream.DEVNULL,
        )
        if off.ok:
            return True
        # `off` fails when the port isn't mapped, so it fails here only if the port still is.
        status = run(
            ["tailscale", "serve", "status", "--json"], env=self.env, stdout=Stream.CAPTURE
        )
        try:
            parsed: object = json.loads(status.stdout)
        except ValueError:
            return True
        # jq's `.Services[$service].TCP[$port] // empty`: null goes through, and anything else
        # but an object stops jq, which counts as not mapped.
        mapping: object = parsed
        for key in ("Services", service, "TCP", public):
            if mapping is None:
                return True
            if not isinstance(mapping, dict):
                return True
            mapping = cast(dict[str, object], mapping).get(key)
        return mapping is None or mapping is False

    def remove_project(self, project: str) -> bool:
        """Removes every container, volume and network of the Compose project, by Compose's own
        label, so it needs neither the template nor its variables."""
        by_project = ["--filter", f"label=com.docker.compose.project={project}"]
        for kind, remove in (
            ("container", ["container", "rm", "--force", "--volumes"]),
            ("volume", ["volume", "rm"]),
            ("network", ["network", "rm"]),
        ):
            listing = ["container", "ls", "--all"] if kind == "container" else [kind, "ls"]
            _, listed = self.docker(*listing, "--quiet", *by_project, stdout=Stream.CAPTURE)
            ids = lines(listed)
            if ids and not self.docker(*remove, *ids, stdout=Stream.DEVNULL)[0]:
                return False
        return True

    def destroy_feature(self, env: str) -> None:
        """Destroys the App's Feature Environment ``env``: its Service mapping, its containers,
        volumes and networks, and then its state, which frees its Slot. A destroy that stops
        partway leaves the Slot held, and running it again finishes the job. Its failures don't
        prune: the Bash ran it in a subshell during a Sweep."""
        directory = self.environments / env
        if not directory.is_dir():
            tell(f"deadlift: {env} isn't on this Server, so there was nothing to destroy")
            return
        slot = _read_text(directory / "slot") if (directory / "slot").is_file() else ""
        say(f"Destroying {env}")
        if slot and not self.unmap_slot(slot):
            raise Stop(
                Exit.FAILED,
                f"couldn't remove {env}'s address from Tailscale Serve, so {env} was left as it "
                "was",
            )
        if not self.remove_project(f"deadlift-{self.app}-{env}"):
            raise Stop(
                Exit.FAILED,
                f"couldn't remove all of {env}'s containers, volumes and networks. It's no "
                "longer mapped, and it keeps its Slot until destroy is run again",
            )
        try:
            _remove_tree(directory)
        except OSError:
            raise Stop(
                Exit.FAILED,
                f"removed {env}'s containers, volumes and networks, but not its state, so it "
                "keeps its Slot until destroy is run again",
            ) from None
        tell(
            f"deadlift: destroyed {env} with its volumes{f', freeing Slot {slot}' if slot else ''}"
        )

    def feature_environments(self) -> list[str]:
        """The App's Feature Environments. One whose destroy stopped partway is still listed,
        because its state goes last."""
        return sorted(path.name for path in self.environments.glob("feature-*") if path.is_dir())

    def read_keep(self, value: str) -> None:
        """Keeps the Feature Environments named by ``value``, keys separated by commas. Refuses
        a key that isn't one, and a list with no key in it: an empty list, as from a failed API
        call, would sweep everything, and only --keep-none may."""
        if self.keep_given:
            usage("give either --keep or --keep-none, and only once")
        self.keep_given = True
        for key in keep_keys(value):
            if not key:
                continue
            if not _KEY.fullmatch(key):
                usage(
                    f"'{key}' in --keep isn't a Feature Environment key: up to 30 of a-z, 0-9 and -"
                )
            self.keep.append(f"feature-{key}")
        if not self.keep:
            self.refuse(
                "--keep lists no key, so nothing was swept or deployed. Give --keep-none to sweep "
                "every Feature Environment"
            )

    def read_keep_none(self) -> None:
        if self.keep_given:
            usage("give either --keep or --keep-none, and only once")
        self.keep_given = True

    def sweep_features(self) -> None:
        """Sweeps the App: destroys every Feature Environment of it that isn't kept, going on
        past one that fails, and outputs the swept keys for `deliver`."""
        self.swept, self.unswept = [], []
        for env in self.feature_environments():
            if env in self.keep:
                continue
            try:
                self.destroy_feature(env)
            except Stop as stop:
                if stop.why is not None:
                    reason(stop.why)
                self.unswept.append(env)
            except OSError as error:
                tell(str(error))
                self.unswept.append(env)
            else:
                self.swept.append(env)
        output("swept", ",".join(env.removeprefix("feature-") for env in self.swept))

    # The Dev Environment's versions and Volume Snapshots.

    def start_environment(self, directory: Path, routed: str) -> bool:
        """Starts the Environment whose files are in ``directory``, which routes to the service
        ``routed``, and waits for that service to be healthy. False when it doesn't start, with
        the reason as the last line of :attr:`up_log`."""
        return self.compose_teed(
            directory, "up", "--detach", "--pull", "never", "--remove-orphans"
        ) and self.compose_teed(
            directory,
            "up",
            "--detach",
            "--pull",
            "never",
            "--no-recreate",
            "--no-deps",
            "--wait",
            "--wait-timeout",
            HEALTH_TIMEOUT,
            routed,
        )

    def restart_version(self, directory: Path) -> bool:
        """Starts the version of an Environment whose files are in ``directory`` again, routing
        to the service its own template names. False when it doesn't start."""
        if not (directory / "compose.yaml").is_file():
            return False
        ok, text = self.compose(
            directory, "config", "--format", "json", stdout=Stream.CAPTURE, stderr=Stream.DEVNULL
        )
        routed = _routed_of(text) if ok else None
        if routed is None:
            return False
        return self.start_environment(directory, routed)

    def project_containers(self, action: str, *args: str) -> bool:
        """Stops, or with `rm --force` removes, every container of the project, whatever
        template started it."""
        _, listed = self.docker(
            "container",
            "ls",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={self.project}",
            stdout=Stream.CAPTURE,
        )
        ids = lines(listed)
        if not ids:
            return True
        return self.docker("container", action, *args, *ids, stdout=Stream.DEVNULL)[0]

    def project_volumes(self) -> list[str] | None:
        """The named volumes of the project, by their full names."""
        ok, listed = self.docker(
            "volume",
            "ls",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={self.project}",
            stdout=Stream.CAPTURE,
        )
        return lines(listed) if ok else None

    def pull_snapshot_image(self) -> None:
        """Pulls the image Volume Snapshots are taken and put back with, if the Server doesn't
        have it."""
        if self.docker(
            "image", "inspect", SNAPSHOT_IMAGE, stdout=Stream.DEVNULL, stderr=Stream.DEVNULL
        )[0]:
            return
        say(f"Pulling {SNAPSHOT_IMAGE}")
        done = run(
            ["docker", "pull", "--quiet", SNAPSHOT_IMAGE],
            env=self.env,
            stdout=Stream.DEVNULL,
            stderr=Stream.CAPTURE,
        )
        if not done.ok:
            self.fail(
                f"couldn't pull {SNAPSHOT_IMAGE}, which takes Volume Snapshots: "
                f"{last_line(done.stderr)}. Nothing was changed"
            )

    @property
    def dev(self) -> Path:
        return self.environments / "dev"

    @property
    def snapshots(self) -> Path:
        return self.dev / "snapshots"

    def take_snapshot(self) -> bool:
        """Takes a Volume Snapshot of every named volume of the project, whose containers must
        be stopped: each volume's contents as <volume>.tar, in a directory of its own under
        snapshots/. Keeps its name in :attr:`snapshot`. One that couldn't be finished is
        removed, so it never counts.

        The tars stream through the helper's stdout and stdin, with no host directory mounted,
        so it runs in the same user namespace as the App's containers on a Server with
        userns-remap: it reads and writes the volumes as their root does, and owners come back
        as they were.
        """
        # Listed first: a snapshot missing a volume because Docker didn't answer would make
        # Recovery remove that volume.
        volumes = self.project_volumes()
        if volumes is None:
            return False
        self.snapshot = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        n = 1
        while (self.snapshots / self.snapshot).exists():
            n += 1
            self.snapshot = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{n}"
        partial = self.snapshots / f"{self.snapshot}.partial"
        partial.mkdir(parents=True, exist_ok=True)
        for volume in volumes:
            if not volume:
                continue
            with (partial / f"{volume}.tar").open("wb") as tar:
                ok = self.docker(
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--volume",
                    f"{volume}:/volume:ro",
                    SNAPSHOT_IMAGE,
                    "tar",
                    "-cf",
                    "-",
                    "-C",
                    "/volume",
                    ".",
                    stdout=tar,
                )[0]
            if not ok:
                _remove_tree(partial)
                return False
        partial.rename(self.snapshots / self.snapshot)
        return True

    def restore_snapshot(self, name: str) -> bool:
        """Puts the Volume Snapshot ``name`` back into the project's named volumes, whose
        containers must be removed. Each volume it holds gets exactly its contents back, and a
        volume made since, which it doesn't hold, is removed."""
        source = self.snapshots / name
        if not name or not source.is_dir():
            return False
        volumes = self.project_volumes()
        if volumes is None:
            return False
        for volume in volumes:
            if not volume or (source / f"{volume}.tar").is_file():
                continue
            if not self.docker("volume", "rm", volume, stdout=Stream.DEVNULL)[0]:
                return False
        tars = sorted(
            (path for path in source.glob("*.tar") if not path.name.startswith(".")), key=str
        )
        for path in tars:
            if not path.is_file():
                continue
            volume = path.name.removesuffix(".tar")
            with path.open("rb") as tar:
                ok = self.docker(
                    "run",
                    "--rm",
                    "--interactive",
                    "--network",
                    "none",
                    "--volume",
                    f"{volume}:/volume",
                    SNAPSHOT_IMAGE,
                    "sh",
                    "-c",
                    "find /volume -mindepth 1 -delete && tar -xf - -C /volume",
                    stdin=tar,
                    stdout=Stream.DEVNULL,
                )[0]
            if not ok:
                return False
        return True

    def prune_snapshots(self) -> None:
        """Keeps the App's configured number of Volume Snapshots, the newest, and also the one
        Dev's current version was first deployed over, which Rollback's --discard-newer-data
        goes back to."""
        needed = _read_text(self.dev / "snapshot") if (self.dev / "snapshot").is_file() else ""
        try:
            ids = sorted(
                (
                    path.name
                    for path in self.snapshots.iterdir()
                    if path.is_dir()
                    and not path.is_symlink()
                    and not path.name.endswith(".partial")
                ),
                reverse=True,
            )
        except OSError:
            ids = []
        for name in ids[self.snapshots_kept :]:
            if name != needed:
                _remove_tree(self.snapshots / name)

    def record_version(self, nxt: Path) -> None:
        """Records the version in ``nxt``, now running, as Dev's current version. The current
        version before it becomes the previous one, which Rollback goes back to. Deploying the
        current version again, the same template and image references, leaves the previous one
        as it was."""
        dev = self.dev
        if (
            (dev / "compose.yaml").is_file()
            and _same(dev / "compose.yaml", nxt / "compose.yaml")
            and _same(dev / "images", nxt / "images")
        ):
            _copy(dev / "snapshot", nxt / "snapshot")
        else:
            (nxt / "snapshot").write_text(f"{self.snapshot}\n")
            if (dev / "compose.yaml").is_file():
                aside = dev / "previous.new"
                _remove_tree(aside)
                aside.mkdir()
                for name in VERSION_FILES:
                    if (dev / name).is_file():
                        _copy(dev / name, aside / name)
                _remove_tree(dev / "previous")
                aside.rename(dev / "previous")
        for name in (*VERSION_FILES, "secret"):
            _copy(nxt / name, dev / f"{name}.new")
            (dev / f"{name}.new").rename(dev / name)

    def recover_dev(self, why_text: str, url: str) -> NoReturn:
        """Recovery, after the new version didn't start: brings Dev down, puts back the Volume
        Snapshot taken before this deploy, and starts the current version again. Always fails,
        saying whether Recovery succeeded and what's left running."""
        why = f"dev didn't start on its new version: {why_text}"
        say("Recovery: putting back dev's Volume Snapshot and its previous version")
        self.project_containers("rm", "--force")
        if not self.restore_snapshot(self.snapshot):
            self.fail(
                f"{why}. Recovery failed: it couldn't put back the Volume Snapshot taken before "
                f"this deploy, {self.snapshot}, so Dev is stopped"
            )
        if not (self.dev / "compose.yaml").is_file():
            self.fail(
                f"{why}. Recovery put back Dev's data as it was before this deploy, and there was "
                "no previous version to start, so Dev is stopped"
            )
        if not self.restart_version(self.dev):
            self.project_containers("stop")
            self.fail(
                f"{why}. Recovery failed: it put back Dev's data as it was before this deploy, "
                f"but the previous version didn't start either ({last_line(self.up_log)}), so "
                "Dev is stopped"
            )
        self.fail(
            f"{why}. Recovery succeeded: Dev is back on its previous version, with its data as "
            f"it was before this deploy, at {url}"
        )

    def deploy_dev(self) -> None:
        """Deploys the delivered template to the Dev Environment, at the base of the App's ports
        and the root of its Service. Nothing stops until the template is checked and every image
        is pulled. Then Dev stops, a Volume Snapshot is taken, and the new version starts. If it
        doesn't, Recovery puts back the snapshot and the previous version."""
        nxt = self.delivery / "next"
        if not (self.delivery / "compose.yaml").is_file():
            self.refuse("the delivery has no compose.yaml to deploy")
        self.project = f"deadlift-{self.app}-dev"
        port = self.port_base

        nxt.mkdir()
        _copy(self.delivery / "compose.yaml", nxt / "compose.yaml")
        (nxt / "images").write_text(
            "".join(f"{line}\n" for line in sorted(f"{n}={r}" for n, r in self.images.items()))
        )
        url = f"https://{self.app}.{self.read_tailnet_suffix()}/"
        self.write_variables(nxt, url)
        self.check_template(nxt)
        self.write_override(nxt, port)
        self.pull_images(again=True)
        self.pull_snapshot_image()

        say("Stopping dev for a Volume Snapshot")
        self.dev.mkdir(parents=True, exist_ok=True)
        self.snapshots.mkdir(parents=True, exist_ok=True)
        if not self.project_containers("stop"):
            if not self.restart_version(self.dev):
                self.fail(
                    "couldn't stop dev for a Volume Snapshot, and its current version didn't "
                    "start again, so Dev is stopped. Its new version wasn't started"
                )
            self.fail(
                "couldn't stop dev for a Volume Snapshot, so its new version wasn't started. Dev "
                f"is up on its current version, at {url}"
            )
        if not self.take_snapshot():
            if not self.restart_version(self.dev):
                self.fail(
                    "couldn't take a Volume Snapshot of dev, so its new version wasn't started, "
                    "and Dev is stopped"
                )
            self.fail(
                "couldn't take a Volume Snapshot of dev, so its new version wasn't started. Dev "
                f"is up on its current version, at {url}"
            )

        say("Starting dev")
        # Whatever happens from here, the snapshot just taken counts toward the App's number, so
        # a run of failed deploys doesn't fill the disk.
        try:
            if not self.start_environment(nxt, self.routed):
                self.recover_dev(last_line(self.up_log), url)
            self.record_version(nxt)
        finally:
            self.prune_snapshots()
        if not self.serve(443, port):
            self.fail(f"dev is up on its new version, but Tailscale Serve couldn't map it at {url}")

        output("environment", "dev")
        output("url", url)
        self.prune_once()
        tell(f"deadlift: deployed dev, which is up at {url}")

    # Pruning images.

    def recorded_images(self, directory: Path) -> list[str] | None:
        """The image references recorded under ``directory``: each Environment's versions record
        theirs in vars.env, as DEADLIFT_IMAGE_<NAME>. None when it couldn't all be read."""
        if not directory.is_dir():
            return []
        refs: list[str] = []
        failed = False

        def failure(_error: OSError) -> None:
            nonlocal failed
            failed = True

        for root, _dirs, files in os.walk(directory, onerror=failure):
            for name in files:
                path = Path(root) / name
                if name != "vars.env" or path.is_symlink() or not path.is_file():
                    continue
                try:
                    text = path.read_bytes().decode("utf-8", errors="surrogateescape")
                except OSError:
                    failed = True
                    continue
                for line in text.split("\n"):
                    matched = re.match(r"DEADLIFT_IMAGE_[A-Z0-9_]*=", line)
                    if matched:
                        refs.append(line[matched.end() :])
        return None if failed else refs

    def prune_images(self, *, quiet: bool) -> bool:
        """Removes the App's images that nothing needs any more. The App's images are those in
        the repositories its Environments' versions, and this deploy, name. An image is needed
        while a version of any App's Environment records it (Dev's current and previous, and
        each Feature Environment's), while any container uses it, running or not, and always
        when it's the Volume Snapshot helper."""
        errors: Target = Stream.DEVNULL if quiet else Stream.INHERIT
        # Each listing is taken whole first: one that failed part way could make a needed image
        # look unneeded.
        recorded = self.recorded_images(self.state / "apps")
        ours = self.recorded_images(self.environments)
        if recorded is None or ours is None:
            return False
        ok, containers = self.docker(
            "container", "ls", "--all", "--quiet", stdout=Stream.CAPTURE, stderr=errors
        )
        if not ok:
            return False
        used = ""
        if containers.split():
            # A container removed since it was listed fails the inspect, but uses no image any
            # more, so the others' answers are enough. Docker also refuses to remove an image a
            # container uses.
            _, used = self.docker(
                "container",
                "inspect",
                "--format",
                "{{.Image}}",
                *containers.split(),
                stdout=Stream.CAPTURE,
                stderr=Stream.DEVNULL,
            )

        needed: set[str] = set()
        for ref in dict.fromkeys([*recorded, SNAPSHOT_IMAGE]):
            if not ref:
                continue
            needed.add(ref)
            # Not on the Server is fine: nothing to keep.
            found, image_id = self.docker(
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                ref,
                stdout=Stream.CAPTURE,
                stderr=Stream.DEVNULL,
            )
            if found:
                needed.add(image_id.rstrip("\n"))
        needed.update(line for line in used.split("\n") if line)
        repositories = sorted({repository_of(ref) for ref in [*ours, *self.images.values()] if ref})

        for repository in repositories:
            ok, listed = self.docker(
                "image",
                "ls",
                "--no-trunc",
                "--format",
                "{{.Repository}} {{.Tag}} {{.Digest}} {{.ID}}",
                repository,
                stdout=Stream.CAPTURE,
                stderr=errors,
            )
            if not ok:
                return False
            for line in listed.split("\n"):
                fields = line.split(None, 3)
                name, tag, digest, image_id = [*fields, "", "", "", ""][:4]
                image_id = image_id.rstrip()
                if not image_id or image_id in needed or f"{name}:{tag}" in needed:
                    continue
                if tag != "<none>":
                    target = f"{name}:{tag}"
                elif digest != "<none>":
                    target = f"{name}@{digest}"
                else:
                    continue
                if not self.docker("image", "rm", target, stdout=Stream.DEVNULL, stderr=errors)[0]:
                    return False
                if not quiet:
                    tell(f"Removed the image {target}, which no Environment uses")
        return True

    def prune_once(self) -> None:
        """Prunes the App's images once a deploy holding the Server's lock has an outcome,
        whatever it is, and before its reason is told, so the reason stays the last line. A
        failed prune is only a warning: the deploy's outcome stands."""
        if not self.prune_pending:
            return
        self.prune_pending = False
        try:
            pruned = self.prune_images(quiet=False)
        except OSError:
            pruned = False
        if not pruned:
            tell(
                "Warning: couldn't remove every image no Environment uses. The next deploy tries "
                "again"
            )

    def prune_quietly(self) -> None:
        """For a way out of a deploy that didn't prune, such as a refused template whose reason
        the template check gave: prunes without a word."""
        if not self.prune_pending:
            return
        self.prune_pending = False
        # A prune is never more than a warning, and this one is silent.
        with contextlib.suppress(Exception):
            self.prune_images(quiet=True)

    # Rollback.

    def promote_previous(self) -> None:
        """Promotes Dev's previous version, now running, to its current version, and forgets the
        one rolled back from: Rollback goes back one step only."""
        previous = self.dev / "previous"
        for name in VERSION_FILES:
            if (previous / name).is_file():
                _copy(previous / name, self.dev / f"{name}.new")
                (self.dev / f"{name}.new").rename(self.dev / name)
            else:
                (self.dev / name).unlink(missing_ok=True)
        _remove_tree(previous)

    def rollback_dev(self) -> None:
        """Rolls the Dev Environment back to its previous version, which becomes its current
        version: one step only. Nothing stops until the previous version's images are on the
        Server. It starts on today's data, and is kept when it's healthy. When it isn't, the
        current version starts again and Rollback is refused, unless --discard-newer-data says
        to discard newer data: then a Volume Snapshot of today's data is taken, and the previous
        version starts on the snapshot from before the current version was first deployed. If
        that fails too, today's data and the current version go back."""
        dev = self.dev
        previous = dev / "previous"
        self.project = f"deadlift-{self.app}-dev"
        if not (dev / "compose.yaml").is_file():
            self.refuse("dev hasn't been deployed, so there's nothing to roll back")
        if not (previous / "compose.yaml").is_file():
            self.refuse(
                "dev has no previous version to roll back to: Rollback goes back one step only, "
                "and only once Dev has had two versions. To go further back, deploy an earlier "
                "commit. Nothing was changed"
            )
        url = "\n".join(
            line.removeprefix("DEADLIFT_URL=")
            for line in (dev / "vars.env").read_text().split("\n")
            if line.startswith("DEADLIFT_URL=")
        ).rstrip("\n")
        done = run(
            self.compose_argv(previous, "config", "--format", "json"),
            env=self.compose_env,
            stdout=Stream.CAPTURE,
            stderr=Stream.CAPTURE,
        )
        if not done.ok:
            self.fail(
                f"couldn't read dev's previous version: {last_line(done.stderr)}. Nothing was "
                "changed"
            )
        routed = _routed_of(done.stdout)
        if routed is None:
            self.fail(
                "couldn't read dev's previous version: it has no x-deadlift.route. Nothing was "
                "changed"
            )
        self.config = cast(dict[str, object], json.loads(done.stdout))
        self.pull_images(again=False)
        before = ""
        if self.discard:
            if (dev / "snapshot").is_file():
                before = _read_text(dev / "snapshot")
            if not (before and (self.snapshots / before).is_dir()):
                self.fail(
                    "the Volume Snapshot from before dev's current version was deployed is gone, "
                    "so --discard-newer-data can't go back to it. Nothing was changed"
                )
            self.pull_snapshot_image()

        say("Stopping dev to start its previous version")
        if not self.project_containers("stop"):
            if not self.restart_version(dev):
                self.fail(
                    "couldn't stop dev to start its previous version, and its current version "
                    "didn't start again, so Dev is stopped"
                )
            self.fail(
                "couldn't stop dev to start its previous version, so nothing was rolled back. "
                f"Dev is up on its current version, at {url}"
            )
        say("Starting dev's previous version on today's data")
        if self.start_environment(previous, routed):
            self.promote_previous()
            output("environment", "dev")
            output("url", url)
            tell(
                "deadlift: rolled dev back to its previous version, on today's data, which is up "
                f"at {url}"
            )
            return
        why = last_line(self.up_log)
        self.project_containers("stop")

        if not self.discard:
            say("Starting dev's current version again")
            if not self.restart_version(dev):
                self.project_containers("stop")
                self.fail(
                    f"dev's previous version didn't start on today's data ({why}), and its "
                    f"current version didn't start again either ({last_line(self.up_log)}), so "
                    "Dev is stopped"
                )
            self.refuse(
                f"dev's previous version didn't start on today's data: {why}. Rollback was "
                "refused, and Dev is up on its current version again, with its data as it was, "
                f"at {url}. To roll back to the data from before the current version was "
                "deployed, discarding what's been written since, run it again with "
                "--discard-newer-data, which keeps a Volume Snapshot of today's data first"
            )

        say("Taking a Volume Snapshot of dev's data as it is today")
        if not self.take_snapshot():
            if not self.restart_version(dev):
                self.fail(
                    f"dev's previous version didn't start on today's data ({why}), and a Volume "
                    "Snapshot of today's data couldn't be taken, so nothing was discarded, and "
                    "Dev is stopped"
                )
            self.fail(
                f"dev's previous version didn't start on today's data ({why}), and a Volume "
                "Snapshot of today's data couldn't be taken, so nothing was discarded. Dev is up "
                f"on its current version, at {url}"
            )
        # Today's snapshot counts toward the App's number from here, whatever happens.
        try:
            say(f"Putting back the Volume Snapshot from before dev's current version, {before}")
            self.project_containers("rm", "--force")
            if not self.restore_snapshot(before):
                why = (
                    f"the Volume Snapshot from before the current version, {before}, couldn't be "
                    "put back"
                )
            elif not self.start_environment(previous, routed):
                why = (
                    "it didn't start on the data from before the current version either: "
                    f"{last_line(self.up_log)}"
                )
            else:
                self.promote_previous()
                output("environment", "dev")
                output("url", url)
                tell(
                    "deadlift: rolled dev back to its previous version, on its data from before "
                    f"the current version, which is up at {url}. Today's data is kept in the "
                    f"Volume Snapshot {self.snapshot}"
                )
                return
            say("Putting back today's data and dev's current version")
            self.project_containers("rm", "--force")
            if not self.restore_snapshot(self.snapshot) or not self.restart_version(dev):
                self.project_containers("stop")
                self.fail(
                    f"dev's previous version didn't start on today's data, and {why}. Putting back "
                    "today's data and the current version failed too, so Dev is stopped. Today's "
                    f"data is in the Volume Snapshot {self.snapshot}"
                )
            self.fail(
                f"dev's previous version didn't start on today's data, and {why}. Nothing was "
                f"rolled back: Dev is up on its current version again, with today's data, at {url}"
            )
        finally:
            self.prune_snapshots()

    # The operations.

    def op_deploy(self, args: Sequence[str]) -> None:
        self.env_name = args[0] if args else ""
        rest = list(args[1:])
        while rest:
            flag = rest[0]
            if flag == "--image":
                matched = _IMAGE.fullmatch(rest[1] if len(rest) > 1 else "")
                if matched is None:
                    usage(
                        "--image needs <name>=<ref>, with a name of lowercase letters, digits and _"
                    )
                if self.images.get(matched[1]):
                    usage(f"the image {matched[1]} is given twice")
                self.images[matched[1]] = matched[2]
                rest = rest[2:]
            elif flag == "--keep":
                if len(rest) < 2:
                    usage("--keep needs the keys of the Feature Environments to keep")
                self.read_keep(rest[1])
                rest = rest[2:]
            elif flag == "--keep-none":
                self.read_keep_none()
                rest = rest[1:]
            else:
                usage(f"unknown argument '{flag}' for deploy")
        if self.env_name.startswith("feature-"):
            self.check_feature_key()
            # The Environment being deployed is always kept.
            self.keep.append(self.env_name)
        elif self.env_name == "dev":
            if self.keep_given:
                usage(
                    "--keep and --keep-none are for a Feature deploy, and dev is the Dev "
                    "Environment"
                )
        else:
            usage(f"'{self.env_name}' isn't an Environment: dev or feature-<key>")
        self.read_app()

        def deploy() -> None:
            self.prune_pending = True
            if self.env_name == "dev":
                self.deploy_dev()
            else:
                self.deploy_feature()

        self.locked(deploy)

    def op_rollback(self, args: Sequence[str]) -> None:
        self.env_name = args[0] if args else ""
        for flag in args[1:]:
            if flag != "--discard-newer-data":
                usage(f"unknown argument '{flag}' for rollback")
            self.discard = True
        if self.env_name.startswith("feature-"):
            self.check_feature_key()
            self.refuse(
                f"Rollback is only for the Dev Environment, dev, and {self.env_name} is a Feature "
                "Environment: deploy it again from an earlier commit instead. Nothing was changed"
            )
        if self.env_name != "dev":
            usage(f"'{self.env_name}' isn't the Dev Environment: rollback takes dev")
        self.read_app()
        self.locked(self.rollback_dev)

    def op_destroy(self, args: Sequence[str]) -> None:
        self.env_name = args[0] if args else ""
        if len(args) > 1:
            usage(f"unknown argument '{args[1]}' for destroy")
        if self.env_name.startswith("feature-"):
            self.check_feature_key()
        elif self.env_name == "dev":
            self.refuse(
                "only Feature Environments can be destroyed, and dev is the Dev Environment. "
                "Nothing was changed"
            )
        else:
            usage(f"'{self.env_name}' isn't a Feature Environment: feature-<key>")
        self.read_app()

        def destroy() -> None:
            output("environment", self.env_name)
            self.destroy_feature(self.env_name)

        self.locked(destroy)

    def op_sweep(self, args: Sequence[str]) -> None:
        rest = list(args)
        while rest:
            flag = rest[0]
            if flag == "--keep":
                if len(rest) < 2:
                    usage("--keep needs the keys of the Feature Environments to keep")
                self.read_keep(rest[1])
                rest = rest[2:]
            elif flag == "--keep-none":
                self.read_keep_none()
                rest = rest[1:]
            else:
                usage(f"unknown argument '{flag}' for sweep")
        if not self.keep_given:
            self.refuse(
                "sweep needs --keep with the keys of the Feature Environments to keep, or "
                "--keep-none to sweep them all. Nothing was swept"
            )
        self.read_app()
        self.locked(self.sweep)

    def sweep(self) -> None:
        self.sweep_features()
        remaining = self.feature_environments()
        if self.unswept:
            self.fail(
                f"couldn't destroy {sentence(self.unswept)} (above)"
                f"{self.swept_clause(', after sweeping ')}. Sweep again to finish"
            )
        if self.swept:
            keeping = f", keeping {sentence(remaining)}" if "".join(remaining) else ""
            tell(f"deadlift: swept {sentence(self.swept)}{keeping}")
        elif remaining:
            tell(
                f"deadlift: swept nothing: {self.app}'s Feature Environments, "
                f"{sentence(remaining)}, are all kept"
            )
        else:
            tell(f"deadlift: swept nothing: {self.app} has no Feature Environments")


def _routed_of(config: str) -> str | None:
    """The service the normalised model ``config`` routes to: its x-deadlift.route up to the
    first colon. None when it has none."""
    try:
        parsed: object = json.loads(config)
    except ValueError:
        return None
    deadlift = (
        cast(dict[str, object], parsed).get("x-deadlift") if isinstance(parsed, dict) else None
    )
    route = cast(dict[str, object], deadlift).get("route") if isinstance(deadlift, dict) else None
    if not isinstance(route, str) or not route:
        return None
    return route.split(":")[0]


class Watchdog(Server):
    """Stops every Feature Environment on the Server when the disk is over the upper
    threshold."""

    def stop_feature(self, env: str, app: str) -> bool:
        """Stops the running containers of the Feature Environment ``env`` of ``app``, keeping
        everything else, so it starts again on its next deploy."""
        ok, listed = self.docker(
            "container",
            "ls",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project=deadlift-{app}-{env}",
            stdout=Stream.CAPTURE,
        )
        if not ok:
            return False
        ids = listed.split()
        if not ids:
            return True
        if not self.docker("container", "stop", *ids, stdout=Stream.DEVNULL)[0]:
            return False
        say(f"Stopped {app}'s {env}")
        return True

    def watch(self) -> None:
        """Every Feature Environment's data, state, Slot and Serve mapping stay, so each starts
        again on its next deploy. Dev Environments are never touched. Goes on past one it can't
        stop, then fails, naming them."""
        _, limit = self.read_settings()
        used = self.disk_used()
        if used is None:
            self.fail("the watchdog couldn't measure how full the disk is, so nothing was stopped")
        if used <= limit:
            tell(
                f"deadlift: the disk is {used}% full, at most the Server's {limit}% limit, so "
                "nothing was stopped"
            )
            return
        say(f"The disk is {used}% full, over {limit}%: stopping every Feature Environment")
        failed: list[str] = []
        for directory in sorted(self.state.glob("apps/*/environments/feature-*"), key=str):
            if not directory.is_dir():
                continue
            env, app = directory.name, directory.parent.parent.name
            if not self.stop_feature(env, app):
                failed.append(f"{app}'s {env}")
        if failed:
            self.fail(
                f"the disk is {used}% full, over the Server's {limit}% limit, and the watchdog "
                f"couldn't stop {sentence(failed)} (above). The other Feature Environments are "
                "stopped, and every Dev Environment is still running"
            )
        tell(
            f"deadlift: the disk is {used}% full, over the Server's {limit}% limit, so the "
            "watchdog stopped every Feature Environment on the Server. Each starts again on its "
            "next deploy, and every Dev Environment is still running"
        )


def _main(argv: Sequence[str]) -> None:
    state = Path(os.environ.get("DEADLIFT_STATE_DIR") or "/srv/deadlift")
    disk_used = os.environ.get("DEADLIFT_DISK_USED", "")
    if disk_used and not _PERCENT.fullmatch(disk_used):
        usage(f"DEADLIFT_DISK_USED is '{disk_used}', and it must be a whole percentage, 0 to 100")

    # The watchdog acts on the whole Server, for its systemd timer, never for a delivery: the
    # entry command doesn't offer it.
    if argv[:1] == ["watchdog"]:
        if len(argv) > 1:
            usage(f"unknown argument '{argv[1]}' for watchdog")
        watchdog = Watchdog(state)
        watchdog.locked(watchdog.watch)
        return

    values = {"app": "", "delivery": ""}
    rest = list(argv)
    while rest and rest[0] in ("--app", "--delivery"):
        if len(rest) < 2:
            usage(f"{rest[0]} needs a value")
        values[rest[0].removeprefix("--")] = rest[1]
        rest = rest[2:]
    if not _APP.fullmatch(values["app"]):
        usage("--app needs an App")
    if not values["delivery"] or not Path(values["delivery"]).is_dir():
        usage("--delivery needs the unpacked delivery")

    operation = App(state, values["app"], Path(values["delivery"]))
    operations: dict[str, Callable[[Sequence[str]], None]] = {
        "deploy": operation.op_deploy,
        "rollback": operation.op_rollback,
        "destroy": operation.op_destroy,
        "sweep": operation.op_sweep,
    }
    command = rest[0] if rest else ""
    if command not in operations:
        usage(f"'{command}' isn't an operation")
    operations[command](rest[1:])


def main(argv: Sequence[str]) -> int:
    os.umask(0o077)
    try:
        _main(argv)
    except Stop as stop:
        if stop.why is not None:
            reason(stop.why)
        return int(stop.code)
    except _Said as said:
        return said.code
    return int(Exit.DONE)
