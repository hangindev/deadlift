"""deadlift's App administration on a Server: the Server half of `deadlift app add`, `app secrets`,
`app remove` and `server status`. Bootstrap installs its launcher, owned by root, at
/usr/local/lib/deadlift/app, and the maintainer's `deadlift` runs it over SSH as root. No App's
CI can run it: a deploy user's one sudo rule is for the entry command only.

Usage:
  app add <app> [--slots <n>] [--snapshots <n>] [--memory <size>] [--cpus <n>] [--pids <n>]
  app secrets <app> dev|feature
  app remove <app>
  app status

add      Gives the App its place on the Server, or brings it up to date, changing only what's
         missing or different, so running it again with the same settings changes nothing:
           - its deploy user, deadlift-<app>, outside the docker group;
           - one sudo rule letting that user run the entry command, and nothing else
             (ADR 0001), checked with visudo;
           - its entry in the Server registry, apps/<app>/app.conf: a block of 100 loopback
             ports overlapping no other App's, kept once given, and its settings. A setting
             not given keeps the value it has, or gets its default on a first add: 3 Slots
             (1 to 9), 3 Volume Snapshots kept, and per container 1g of memory, 1 CPU and 512
             processes.
         Prints `deadlift-output: slots=<n>`, for the App's Service to list every Slot's port.
secrets  Puts the App's secrets file for its Dev or its Feature Environments, read from stdin,
         on the Server as apps/<app>/dev.env or feature.env, only for root. Its variables reach
         the template from each deploy of that kind on.
remove   Removes everything of the App from the Server: its sudo rule, its Service's Serve
         mappings, every Environment with its containers, volumes and networks, its Volume
         Snapshots, secrets files and registry entry, and its deploy user. Done, changing
         nothing, when there's nothing of it left.
status   Shows the Server's disk usage, and each App's ports, Slots in use, Volume Snapshots
         and Environments.

add, secrets and remove take the Server's lock, so they never run alongside an operation.

Every reason line starts with `deadlift: `. Exit codes: 0 done, 1 failed, 2 refused or bad usage
(nothing was changed).
"""

import contextlib
import grp
import json
import os
import pwd
import re
import shutil
import sys
from collections.abc import Sequence, Set
from pathlib import Path
from typing import NoReturn, cast

from deadlift.core.lock import server_lock
from deadlift.core.names import app_name_problem
from deadlift.core.outcome import (
    fail,
    output,
    reason,
    refuse,
    refuse_usage,
    run_main,
    say,
)
from deadlift.core.process import Completed, Stream, run, scrubbed_env
from deadlift.server import host
from deadlift.server.layout import layout
from deadlift.server.text import sentence

STATE = layout().state
ENTRY_COMMAND = Path("/usr/local/sbin/deadlift-entry")
SUDOERS = Path("/etc/sudoers.d")
# Where the Apps' port blocks go: 20000-20099, 20100-20199 and so on.
FIRST_PORT_BASE = 20000
LAST_PORT_BASE = 29900
MAX_SECRETS_BYTES = 1024 * 1024
DEFAULT_SLOTS = "3"
DEFAULT_SNAPSHOTS = "3"

USAGE = """\
Usage: app add <app> [--slots <n>] [--snapshots <n>] [--memory <size>] [--cpus <n>] [--pids <n>]
Usage: app secrets <app> dev|feature
Usage: app remove <app>
Usage: app status
"""


# add's settings: each one's form, and what it is when it isn't that.
_ADD_SETTINGS: dict[str, tuple[re.Pattern[str], str]] = {
    "slots": (re.compile(r"[1-9]"), "--slots is a number of Slots from 1 to 9"),
    "snapshots": (
        re.compile(r"[1-9][0-9]{0,2}"),
        "--snapshots is a number of Volume Snapshots to keep, at least 1",
    ),
    "memory": (re.compile(r"[1-9][0-9]*[kmg]?"), "--memory is a size such as 512m or 2g"),
    "cpus": (
        re.compile(r"(?!0+(\.0+)?$)[0-9]+(\.[0-9]+)?"),
        "--cpus is a number of CPUs above 0, such as 0.5 or 2",
    ),
    "pids": (re.compile(r"[1-9][0-9]*"), "--pids is a number of processes, at least 1"),
}


class BadUsage(Exception):
    """The command line is wrong: refused, with the usage."""


def free_port_base(taken: Set[str]) -> int | None:
    """The lowest block of ports whose base isn't in ``taken`` (other Apps' ``port_base``
    settings), or None when every one is."""
    for base in range(FIRST_PORT_BASE, LAST_PORT_BASE + 1, 100):
        if str(base) not in taken:
            return base
    return None


def add_settings(args: Sequence[str]) -> dict[str, str]:
    """The settings ``add`` is given (``--slots 2`` and so on), by name; a later one wins and an
    empty one counts as not given. Raises BadUsage for anything else, or a value of the wrong
    form."""
    given: dict[str, str] = {}
    rest = list(args)
    while rest:
        flag = rest.pop(0)
        name = flag.removeprefix("--")
        if not flag.startswith("--") or name not in _ADD_SETTINGS:
            raise BadUsage(f"unknown argument '{flag}' for add")
        if not rest:
            raise BadUsage(f"{flag} needs a value")
        given[name] = rest.pop(0)
    for name, (form, what) in _ADD_SETTINGS.items():
        value = given.get(name, "")
        if value and not form.fullmatch(value):
            raise BadUsage(what)
    return {name: value for name, value in given.items() if value}


def setting(conf: str, name: str) -> str:
    """The last value of the setting ``name`` in an app.conf's text, or ""."""
    values = [line[len(name) + 1 :] for line in conf.splitlines() if line.startswith(f"{name}=")]
    return values[-1] if values else ""


def json_object(text: str) -> dict[str, object] | None:
    """``text`` read as a JSON object, or None when it isn't one."""
    try:
        parsed: object = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    return cast(dict[str, object], parsed)


def serve_has_service(status: str, service: str) -> bool:
    """Whether ``tailscale serve status --json``'s answer has mappings for ``service``."""
    parsed = json_object(status)
    services = None if parsed is None else parsed.get("Services")
    if not isinstance(services, dict):
        return False
    found: object = cast(dict[str, object], services).get(service)
    return found is not None and found is not False


# What's below acts on the Server.

_ENV = scrubbed_env(keep=("PATH", "HOME", "DOCKER_HOST"))


def _usage(why: str) -> NoReturn:
    refuse_usage(why, USAGE)


def _refuse(why: str) -> NoReturn:
    refuse(f"{why}. Nothing was changed.")


def _check_app(name: str) -> None:
    problem = app_name_problem(name)
    if problem is not None:
        _usage(problem)


def _int(text: str) -> int:
    """A number read from a file, or 0 (as Bash's arithmetic took what wasn't one)."""
    return int(text) if re.fullmatch(r"\s*[0-9]+\s*", text) else 0


def _read(path: Path) -> str:
    """A file's text with trailing line breaks dropped, as Bash's $(<file) reads it, or "" when
    it can't be read."""
    try:
        return path.read_text().rstrip("\n")
    except (OSError, UnicodeDecodeError):
        return ""


def _setting_of(conf: Path, name: str) -> str:
    try:
        return setting(conf.read_text(errors="replace"), name)
    except OSError:
        return ""


def _docker_lines(*args: str) -> list[str]:
    """The non-empty lines ``docker <args>`` prints (none when it fails)."""
    done = run(["docker", *args], env=_ENV, stdout=Stream.CAPTURE)
    return [line for line in done.stdout.splitlines() if line]


def _must(argv: Sequence[str], *, stdout: Stream = Stream.STDERR) -> Completed:
    """Runs ``argv``, stopping with a failure when it fails, as Bash's set -e did."""
    done = run(argv, env=_ENV, stdout=stdout)
    if not done.ok:
        fail(f"{' '.join(argv)} failed with exit code {done.code}")
    return done


def _ensure_dir(path: Path, mode: int) -> None:
    """``install -d -m <mode> -o root -g root``: makes ``path`` if need be, root's, with
    ``mode``."""
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    status = path.stat()
    if (status.st_mode & 0o7777) != mode:
        path.chmod(mode)
    if (status.st_uid, status.st_gid) != (0, 0):
        os.chown(path, 0, 0)


def _write_aside(target: Path, content: bytes, mode: int) -> Path:
    """Writes ``content`` beside ``target`` as <target>.new, root's, with ``mode``."""
    aside = target.with_name(f"{target.name}.new")
    aside.unlink(missing_ok=True)
    fd = os.open(aside, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(content)
    os.chown(aside, 0, 0)
    aside.chmod(mode)
    return aside


def _owned_by_root_with(path: Path, mode: int) -> bool:
    try:
        status = path.lstat()
    except OSError:
        return False
    return (status.st_mode & 0o7777) == mode and (status.st_uid, status.st_gid) == (0, 0)


def _write_file(path: Path, content: str, mode: int) -> bool:
    """Writes ``content`` (a line break added) to ``path`` with ``mode``, owned by root, unless
    it holds exactly that already. Written aside and renamed, so it's never half there. Returns
    whether it wrote."""
    if (
        path.is_file()
        and not path.is_symlink()
        and _read(path) == content
        and _owned_by_root_with(path, mode)
    ):
        return False
    _write_aside(path, f"{content}\n".encode(), mode).replace(path)
    return True


def _user_exists(user: str) -> bool:
    try:
        pwd.getpwnam(user)
    except KeyError:
        return False
    return True


def _groups_of(user: str) -> set[str]:
    entry = pwd.getpwnam(user)
    names: set[str] = set()
    for gid in os.getgrouplist(user, entry.pw_gid):
        with contextlib.suppress(KeyError):
            names.add(grp.getgrgid(gid).gr_name)
    return names


def _highest_held_slot(app: str) -> int:
    """The highest Slot one of the App's Feature Environments holds, or 0."""
    highest = 0
    for file in (STATE / "apps" / app / "environments").glob("feature-*/slot"):
        if file.is_file():
            highest = max(highest, _int(_read(file)))
    return highest


def op_add(args: Sequence[str]) -> None:
    app = args[0] if args else ""
    _check_app(app)
    try:
        given = add_settings(args[1:])
    except BadUsage as error:
        _usage(str(error))
    if not os.access(ENTRY_COMMAND, os.X_OK):
        _refuse("this Server has no entry command. Run `deadlift server bootstrap` first")

    with server_lock(STATE):
        _ensure_dir(STATE, 0o700)
        directory = STATE / "apps" / app
        conf = directory / "app.conf"
        _ensure_dir(STATE / "apps", 0o700)
        settings = dict(given)
        if conf.is_file():
            port_base = _int(_setting_of(conf, "port_base"))
            for name in ("slots", "snapshots", "memory", "cpus", "pids"):
                if name not in settings:
                    settings[name] = _setting_of(conf, name)
        else:
            taken = {
                _setting_of(other, "port_base")
                for other in (STATE / "apps").glob("*/app.conf")
                if other.is_file()
            }
            base = free_port_base(taken)
            if base is None:
                _refuse(
                    f"every block of ports from {FIRST_PORT_BASE} to {LAST_PORT_BASE + 99} is"
                    " another App's"
                )
            port_base = base
        slots = settings.get("slots") or DEFAULT_SLOTS
        snapshots = settings.get("snapshots") or DEFAULT_SNAPSHOTS
        highest = _highest_held_slot(app)
        if highest > _int(slots):
            _refuse(
                f"{app}'s Feature Environments hold Slots up to {highest}, more than {slots}."
                " Destroy them first, or keep more Slots"
            )

        what: list[str] = []
        # The deploy user, outside the docker group: only the entry command acts on Docker for it.
        user = f"deadlift-{app}"
        if not _user_exists(user):
            _must(
                [
                    "useradd",
                    "--create-home",
                    "--user-group",
                    "--shell",
                    "/bin/bash",
                    "--comment",
                    f"deadlift deploy user of {app}",
                    user,
                ]
            )
            what.append(f"created the deploy user {user}")
        if "docker" in _groups_of(user):
            _must(["gpasswd", "--delete", user, "docker"], stdout=Stream.DEVNULL)
            what.append(f"took {user} out of the docker group")

        # The one sudo rule. sudo ignores files in sudoers.d with a dot in their name, so the new
        # one isn't live until it's checked and renamed.
        rule = SUDOERS / user
        wanted = f"{user} ALL=(root) NOPASSWD: {ENTRY_COMMAND}"
        if _read(rule) != wanted or not _owned_by_root_with(rule, 0o440):
            aside = _write_aside(rule, f"{wanted}\n".encode(), 0o440)
            checked = run(
                ["visudo", "--check", "--quiet", "--file", str(aside)],
                env=_ENV,
                stdout=Stream.DEVNULL,
            )
            if not checked.ok:
                aside.unlink(missing_ok=True)
                fail(f"the sudo rule for {user} didn't pass visudo, so it wasn't installed")
            aside.replace(rule)
            what.append(f"installed {user}'s sudo rule for the entry command")
        check = run(["visudo", "--check"], env=_ENV, stdout=Stream.CAPTURE, stderr=Stream.CAPTURE)
        if not check.ok:
            found = (check.stdout + check.stderr).rstrip("\n").replace("\n", " ")
            fail(f"visudo finds the Server's sudo rules broken after adding {user}'s: {found} ")

        # The App's entry in the Server registry.
        _ensure_dir(directory, 0o700)
        lines = [f"port_base={port_base}", f"slots={slots}", f"snapshots={snapshots}"]
        lines += [
            f"{name}={settings[name]}" for name in ("memory", "cpus", "pids") if settings.get(name)
        ]
        if _write_file(conf, "\n".join(lines), 0o600):
            what.append("set its settings in the Server registry")

        output("slots", slots)
        summary = (
            f"ports {port_base}-{port_base + 99}, {slots} Slots, {snapshots} Volume Snapshots kept"
        )
        if what:
            reason(f"{app} is on this Server, with {summary}. This run {sentence(what)}.")
        else:
            reason(f"{app} is on this Server, with {summary}. Nothing needed changing.")


def op_secrets(args: Sequence[str]) -> None:
    app = args[0] if args else ""
    kind = args[1] if len(args) > 1 else ""
    _check_app(app)
    if kind not in ("dev", "feature"):
        _usage(f"secrets is for dev or feature, not '{kind}'")
    if len(args) != 2:
        _usage("secrets takes <app> dev|feature, and the file on stdin")
    directory = STATE / "apps" / app
    file = directory / f"{kind}.env"
    with server_lock(STATE):
        _ensure_dir(STATE, 0o700)
        if not (directory / "app.conf").is_file():
            _refuse(f"{app} isn't an App on this Server. Run `deadlift app add` first")
        content = sys.stdin.buffer.read(MAX_SECRETS_BYTES + 1)
        if len(content) > MAX_SECRETS_BYTES:
            _refuse(f"the secrets file is over {MAX_SECRETS_BYTES} bytes")
        try:
            same = file.is_file() and file.read_bytes() == content
        except OSError:
            same = False
        if same:
            reason(f"{app}'s {kind}.env on this Server already holds that. Nothing was changed.")
            return
        _write_aside(file, content, 0o600).replace(file)
        environment = "Dev" if kind == "dev" else "Feature"
        reason(
            f"put {app}'s {kind}.env on this Server. Its variables reach the template from the"
            f" next deploy of a {environment} Environment on."
        )


def op_remove(args: Sequence[str]) -> None:
    app = args[0] if args else ""
    _check_app(app)
    if len(args) != 1:
        _usage("remove takes only <app>")
    user = f"deadlift-{app}"
    rule = SUDOERS / user
    directory = STATE / "apps" / app
    with server_lock(STATE):
        _ensure_dir(STATE, 0o700)
        info = run(["docker", "info"], env=_ENV, stdout=Stream.DEVNULL, stderr=Stream.DEVNULL)
        if not info.ok:
            fail(f"Docker isn't answering on this Server, so nothing of {app} was removed")
        what: list[str] = []

        # First the way in, so the App's CI can't start anything while the rest goes.
        if rule.exists():
            rule.unlink()
            what.append("its sudo rule")

        # Then its addresses. Tailscale Serve keeps them per Service, so only this App's go.
        serve = run(["tailscale", "serve", "status", "--json"], env=_ENV, stdout=Stream.CAPTURE)
        if not serve.ok:
            fail(
                f"couldn't read Tailscale Serve's mappings, so {app}'s Environments were left as"
                " they were. Its sudo rule is gone, so its CI can't reach them; run `deadlift app"
                " remove` again"
            )
        if serve_has_service(serve.stdout, f"svc:{app}"):
            if not run(["tailscale", "serve", "clear", f"svc:{app}"], env=_ENV).ok:
                fail(
                    f"couldn't remove {app}'s Service from Tailscale Serve, so its Environments"
                    " were left as they were. Run `deadlift app remove` again"
                )
            what.append("its Service's mappings")

        # Every Environment's Compose project: the ones deadlift has state for, and any Docker
        # still has by deadlift's label, so a half-finished deploy or destroy is cleaned up too.
        projects: list[str] = []
        environments = directory / "environments"
        if environments.is_dir():
            projects += [
                host.project_name(app, env.name)
                for env in sorted(environments.iterdir())
                if not env.name.startswith(".") and env.is_dir()
            ]
        project_label = '{{.Label "com.docker.compose.project"}}'
        by_app = ["--filter", f"label=deadlift.app={app}", "--format", project_label]
        projects += _docker_lines("container", "ls", "--all", *by_app)
        projects += _docker_lines("volume", "ls", *by_app)
        for project in sorted(set(projects)):
            if not host.remove_project(project, _ENV):
                fail(
                    f"couldn't remove all of {app}'s containers, volumes and networks (in"
                    f" {project}). Run `deadlift app remove` again"
                )
        # A volume carrying the App's label outside any project, should one ever exist.
        ids = _docker_lines("volume", "ls", "--quiet", "--filter", f"label=deadlift.app={app}")
        if ids and not run(["docker", "volume", "rm", *ids], env=_ENV, stdout=Stream.DEVNULL).ok:
            fail(f"couldn't remove {app}'s volumes. Run `deadlift app remove` again")
        if projects:
            what.append("its Environments with their volumes")

        # Its Volume Snapshots, secrets files and registry entry.
        if directory.is_dir():
            if directory.is_symlink():
                directory.unlink()
            else:
                shutil.rmtree(directory)
            what.append("its registry entry with its secrets files and Volume Snapshots")

        # Last, its deploy user, with anything still running as it, such as a closing SSH session.
        if _user_exists(user):
            run(["pkill", "--signal", "KILL", "--uid", user], env=_ENV, stderr=Stream.DEVNULL)
            removed = run(["userdel", "--force", "--remove", user], env=_ENV, stderr=Stream.DEVNULL)
            if not removed.ok:
                fail(f"couldn't remove the deploy user {user}. Run `deadlift app remove` again")
            what.append(f"its deploy user {user}")

        if what:
            reason(f"removed {app} from this Server: {sentence(what)}.")
        else:
            reason(f"{app} isn't on this Server, so there was nothing to remove.")


def op_status(args: Sequence[str]) -> None:
    if args:
        _usage("status takes no arguments")
    suffix = host.magic_dns_suffix(_ENV, errors=Stream.DEVNULL)
    settings = STATE / "settings"
    deploy_limit = _setting_of(settings, "feature_deploy_disk_limit")
    watchdog_limit = _setting_of(settings, "watchdog_disk_limit")
    used = host.disk_used(STATE, _ENV) or 0
    say(
        f"Disk: {used}% used. Feature deploys are skipped over {deploy_limit}%, and the"
        f" watchdog stops Feature Environments over {watchdog_limit}%."
    )
    apps = 0
    app_dirs = sorted(path for path in (STATE / "apps").glob("*") if not path.name.startswith("."))
    for app_dir in app_dirs:
        conf = app_dir / "app.conf"
        if not app_dir.is_dir() or not conf.is_file():
            continue
        app = app_dir.name
        apps += 1
        slots = _setting_of(conf, "slots")
        environments = app_dir / "environments"
        held = 0
        snaps = 0
        with contextlib.suppress(OSError):
            for env in os.scandir(environments):
                if env.name.startswith("feature-") and env.is_dir(follow_symlinks=False):
                    held += os.path.lexists(Path(env.path) / "slot")
        with contextlib.suppress(OSError):
            for snapshot in os.scandir(environments / "dev" / "snapshots"):
                if snapshot.is_dir(follow_symlinks=False) and not snapshot.name.endswith(
                    ".partial"
                ):
                    snaps += 1
        port_base = _setting_of(conf, "port_base")
        say()
        say(
            f"{app}: ports {port_base}-{_int(port_base) + 99}, Slots in use: {held} of {slots},"
            f" Volume Snapshots: {snaps}"
        )
        line = False
        env_dirs = sorted(path for path in environments.glob("*") if not path.name.startswith("."))
        for env_dir in env_dirs:
            if not env_dir.is_dir():
                continue
            env = env_dir.name
            slot = ""
            url = ""
            if env == "dev":
                url = f"https://{app}.{suffix}/" if suffix else ""
            elif (env_dir / "slot").is_file():
                slot = _read(env_dir / "slot")
                url = f"https://{app}.{suffix}:{10000 + _int(slot)}/" if suffix else ""
            project = host.by_project(host.project_name(app, env))
            total = len(_docker_lines("container", "ls", "--all", "--quiet", *project))
            running = len(_docker_lines("container", "ls", "--quiet", *project))
            counted = f"{running} of {total} running"
            slotted = f"Slot {slot}" if slot else ""
            say(f"  {env:<36} {counted:<22} {slotted:<8} {url}")
            line = True
        if not line:
            say("  no Environments")
    if apps == 0:
        say()
        say("No Apps. Add one with `deadlift app add`.")


def _main(argv: Sequence[str]) -> None:
    if os.geteuid() != 0:
        _refuse("app needs root")
    command = argv[0] if argv else ""
    match command:
        case "add":
            op_add(argv[1:])
        case "secrets":
            op_secrets(argv[1:])
        case "remove":
            op_remove(argv[1:])
        case "status":
            op_status(argv[1:])
        case _:
            _usage(f"'{command}' isn't a command: add, secrets, remove or status")


def main(argv: Sequence[str]) -> int:
    os.umask(0o077)
    return run_main(lambda: _main(argv))
