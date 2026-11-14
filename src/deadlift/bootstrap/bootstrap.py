"""deadlift's bootstrap: turns a fresh Ubuntu 24.04 machine into a Server. `deadlift server
bootstrap <server>` sends it over SSH with the rest of deadlift's Server code and runs it as root,
so you rarely run it yourself. By hand, as root, with <dir> holding what that command sends
(deadlift/):

  python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); \\
    from deadlift.bootstrap.bootstrap import main; sys.exit(main(sys.argv[1:]))' <dir>

It's safe to run again, and running it again is how a Server catches up with deadlift: a fix to
the entry command or the deploy script reaches a Server only this way, never with a delivery
(ADR 0001). Each step checks first and changes only what's missing or different, so a run with
nothing to do changes no file and restarts nothing: not Docker, and no Environment.

What it sets up:
  - Docker Engine with the Compose plugin, from Docker's apt repository, running with
    `userns-remap`, so root in a container is an unprivileged user on the host. Once installed,
    Docker is never upgraded here: that would restart every Environment. Turning on
    `userns-remap` restarts Docker once, and the daemon then keeps its images and volumes apart
    from the ones it had before.
  - Tailscale, from its apt repository, unless it's installed. Joining the tailnet is the
    provision wizard's step, not this one.
  - deadlift's Server code, the Python package deadlift with its core and server parts, owned by
    root at /usr/local/lib/deadlift/python, and a launcher for each of its entry points (ADR
    0004). python3 with PyYAML, which they use.
  - The firewall for containers, at /usr/local/lib/deadlift/firewall, applied now and by
    deadlift-firewall.service at every boot before Docker starts (see deadlift.server.firewall).
  - The entry command, /usr/local/sbin/deadlift-entry, and the deploy script it runs,
    /usr/local/lib/deadlift/deploy, with the template check, template, beside it, all
    owned by root. `deadlift app add` gives each App's deploy user one sudo rule for the entry
    command.
  - The Server half of `deadlift app add`, `app secrets`, `app remove` and `server status`,
    /usr/local/lib/deadlift/app, which the maintainer runs over SSH as root.
  - /srv/deadlift, only for root: the Server's lock, the Server registry (apps/) and the
    Server-wide settings (settings, below).
  - The watchdog's timer, deadlift-watchdog.timer, which runs `deploy watchdog` every 5
    minutes (deadlift-watchdog.service): over the upper disk threshold, it stops every Feature
    Environment on the Server and leaves every Dev Environment running.

What earlier versions installed and deadlift no longer uses is removed: the entry points in
/usr/local/lib/deadlift by their old names, deploy.sh, template.sh, app.sh and firewall.sh.

The Server-wide settings, in /srv/deadlift/settings, one `name=value` a line. A setting missing
from the file is added with its default; one already set is never changed, so edit the file to
change one:
  feature_deploy_disk_limit  Over this percentage of disk used, Feature deploys are skipped. 80.
  watchdog_disk_limit        Over this percentage, the watchdog stops every Feature Environment
                             on the Server. 90.

Every reason line starts with `deadlift: `. Exit codes: 0 done, 1 failed, 2 refused (nothing
was changed).

Standard library only (and deadlift.core): it runs before python3-yaml is installed.
"""

import json
import os
import shlex
import shutil
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn, cast

from deadlift.bootstrap.install import (
    LAUNCHERS,
    SERVER_LIB,
    SERVER_SBIN,
    Launcher,
    install_bytes,
    install_launchers,
    install_package,
    python_dir,
    remove_retired_launchers,
)
from deadlift.core.lock import server_lock
from deadlift.core.outcome import fail, refuse, run_main
from deadlift.core.process import Env, Stream, run, scrubbed_env

STATE = Path("/srv/deadlift")
DAEMON_JSON = Path("/etc/docker/daemon.json")
FIREWALL_UNIT = Path("/etc/systemd/system/deadlift-firewall.service")
WATCHDOG_UNIT = Path("/etc/systemd/system/deadlift-watchdog")
OS_RELEASE = Path("/etc/os-release")
SETTINGS_DEFAULTS = ("feature_deploy_disk_limit=80", "watchdog_disk_limit=90")

# What each installed entry point is, for the log. One with no line here is installed quietly.
INSTALLED_WHAT: dict[Path, str] = {
    SERVER_SBIN / "deadlift-entry": "the entry command",
    SERVER_LIB / "deploy": "the deploy script",
    SERVER_LIB / "template": "the template check beside it",
    SERVER_LIB / "app": "the Server half of `deadlift app` and `server status`",
}

SETTINGS_HEADER = """\
# deadlift's Server-wide settings. See `deadlift server bootstrap`'s
# bootstrap.py for what each means. Bootstrap adds a missing one and never changes one set here."""


def os_release(text: str) -> dict[str, str]:
    """The variables of /etc/os-release's ``text``, unquoted as a shell would."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            continue
        for word in words:
            name, sep, value = word.partition("=")
            if sep and name:
                found[name] = value
    return found


def codename(release: Mapping[str, str]) -> str:
    """The suite name of this Ubuntu release, e.g. noble."""
    return release.get("UBUNTU_CODENAME") or release.get("VERSION_CODENAME") or ""


def remapped_docker_config(text: str | None) -> str | None:
    """Docker's config (``text``, None when there's none) with ``userns-remap`` turned on, as it
    should be written, or None when it's on already. Raises ValueError when ``text`` isn't a
    JSON object.

    An empty file gives an empty config, as the Bash did with jq (see followups).
    """
    if text is None:
        text = "{}"
    if not text.strip():
        return ""
    parsed: object = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Docker's config isn't a JSON object")
    config = cast(dict[str, object], parsed)
    remap = config.get("userns-remap")
    if isinstance(remap, str) and remap:
        return None
    return json.dumps({**config, "userns-remap": "default"}, indent=2, ensure_ascii=False)


def missing_settings(text: str) -> list[str]:
    """The Server-wide settings, ``name=default``, that the settings file's ``text`` lacks."""
    lines = text.splitlines()
    return [
        default
        for default in SETTINGS_DEFAULTS
        if not any(line.startswith(f"{default.partition('=')[0]}=") for line in lines)
    ]


# What's below acts on the machine.


def _env() -> Env:
    return scrubbed_env()


def _say(line: str) -> None:
    sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


def _step(what: str) -> None:
    _say(f"==> {what}")


def _refuse(why: str) -> NoReturn:
    refuse(f"{why}. Nothing was changed.")


def _ok(argv: Sequence[str]) -> bool:
    """Whether ``argv`` succeeds, quietly."""
    done = run(argv, env=_env(), stdout=Stream.DEVNULL, stderr=Stream.DEVNULL)
    return done.ok


def _must(argv: Sequence[str], *, env: Env | None = None) -> None:
    """Runs ``argv``, its output shown, stopping with a failure when it fails."""
    done = run(argv, env=_env() if env is None else env, stdout=Stream.INHERIT)
    if not done.ok:
        fail(f"{' '.join(argv)} failed with exit code {done.code}")


def _which(program: str) -> str | None:
    return shutil.which(program, path=_env().variables["PATH"])


def _install_content(content: str, target: Path, mode: int) -> bool:
    """Installs a file at ``target`` holding ``content`` and a line break, owned by root, unless
    it's there already just so. Returns whether it wrote."""
    return install_bytes(f"{content}\n".encode(), target, mode, chown=True)


def _install_dir(path: Path, mode: int) -> None:
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    status = path.stat()
    if (status.st_mode & 0o7777) != mode:
        path.chmod(mode)
    if (status.st_uid, status.st_gid) != (0, 0):
        os.chown(path, 0, 0)


def _installed_or_kept(changed: bool) -> str:
    return "Installed" if changed else "Kept"


class _Apt:
    """apt-get, updating its package lists once before the first install."""

    def __init__(self) -> None:
        self.updated = False

    def install(self, *packages: str) -> None:
        if not self.updated:
            _must(["apt-get", "update", "-qq"])
            self.updated = True
        _must(
            ["apt-get", "install", "-qq", "--yes", "--no-install-recommends", *packages],
            env=_env().with_vars({"DEBIAN_FRONTEND": "noninteractive"}),
        )


def _release() -> dict[str, str]:
    try:
        return os_release(OS_RELEASE.read_text())
    except OSError:
        return {}


def install_prerequisites(apt: _Apt) -> None:
    missing: list[str] = []
    # curl for Docker's and Tailscale's apt keys; flock and the rest come with Ubuntu.
    if _which("curl") is None:
        missing.append("curl")
    # PyYAML, for the template check to read a template's YAML before Compose does.
    if not _ok(["python3", "-I", "-c", "import yaml"]):
        missing += ["python3", "python3-yaml"]
    if missing:
        apt.install("ca-certificates", *missing)
    _say("curl and python3 with PyYAML are installed")


def configure_docker() -> None:
    """Turns on userns-remap in Docker's config, keeping what else it holds. Done before Docker
    is installed, so a fresh Server's Docker starts remapped and never needs a restart."""
    _install_dir(DAEMON_JSON.parent, 0o755)
    try:
        current = DAEMON_JSON.read_text() if DAEMON_JSON.is_file() else None
        wanted = remapped_docker_config(current)
    except (ValueError, OSError):
        fail(f"{DAEMON_JSON} isn't valid JSON, so Docker's config was left alone")
    if wanted is None:
        _say("Docker's config has userns-remap")
        return
    _install_content(wanted, DAEMON_JSON, 0o644)
    _say(f"Turned on userns-remap in {DAEMON_JSON}")


def install_docker(apt: _Apt) -> None:
    if _ok(["docker", "compose", "version"]):
        _say("Docker and Compose are installed")
        return
    # https://docs.docker.com/engine/install/ubuntu/
    apt.install("ca-certificates", "curl")
    keyrings = Path("/etc/apt/keyrings")
    keyrings.mkdir(mode=0o755, parents=True, exist_ok=True)
    keyrings.chmod(0o755)
    key = keyrings / "docker.asc"
    _must(["curl", "-fsSL", "https://download.docker.com/linux/ubuntu/gpg", "-o", str(key)])
    key.chmod((key.stat().st_mode & 0o7777) | 0o444)
    arch = run(["dpkg", "--print-architecture"], env=_env(), stdout=Stream.CAPTURE)
    if not arch.ok:
        fail(f"dpkg --print-architecture failed with exit code {arch.code}")
    Path("/etc/apt/sources.list.d/docker.list").write_text(
        f"deb [arch={arch.stdout.strip()} signed-by={key}]"
        f" https://download.docker.com/linux/ubuntu {codename(_release())} stable\n"
    )
    apt.updated = False
    apt.install(
        "docker-ce",
        "docker-ce-cli",
        "containerd.io",
        "docker-buildx-plugin",
        "docker-compose-plugin",
    )


def install_tailscale(apt: _Apt) -> None:
    if _which("tailscale") is not None:
        _say("Tailscale is installed")
        return
    # https://tailscale.com/kb/1187/install-ubuntu-2204, for this release's codename.
    apt.install("ca-certificates", "curl")
    suite = codename(_release())
    _must(
        [
            "curl",
            "-fsSL",
            f"https://pkgs.tailscale.com/stable/ubuntu/{suite}.noarmor.gpg",
            "-o",
            "/usr/share/keyrings/tailscale-archive-keyring.gpg",
        ]
    )
    _must(
        [
            "curl",
            "-fsSL",
            f"https://pkgs.tailscale.com/stable/ubuntu/{suite}.tailscale-keyring.list",
            "-o",
            "/etc/apt/sources.list.d/tailscale.list",
        ]
    )
    apt.updated = False
    apt.install("tailscale")
    _must(["systemctl", "enable", "--now", "tailscaled"])


def _launcher_path(launcher: Launcher) -> Path:
    return (SERVER_SBIN if launcher.where == "sbin" else SERVER_LIB) / launcher.name


def _log_installed(changed: bool, target: Path) -> None:
    what = INSTALLED_WHAT.get(target)
    if what is not None:
        _say(f"{_installed_or_kept(changed)} {what}, {target}")


def install_scripts(payload: Path) -> None:
    _install_dir(SERVER_LIB, 0o755)
    changed = install_package(payload / "deadlift", SERVER_LIB, chown=True)
    _say(f"{_installed_or_kept(changed)} deadlift's Server code, {python_dir(SERVER_LIB)}")
    for launcher in LAUNCHERS:
        target = _launcher_path(launcher)
        changed = install_launchers((launcher,), lib=SERVER_LIB, sbin=SERVER_SBIN, chown=True)
        _log_installed(changed, target)


def start_and_enable(unit: str) -> None:
    """Starts and enables a unit, unless it's both already."""
    if not _ok(["systemctl", "is-enabled", "--quiet", unit]):
        _must(["systemctl", "enable", "--quiet", unit])
    if not _ok(["systemctl", "is-active", "--quiet", unit]):
        _must(["systemctl", "start", unit])


def setup_firewall() -> None:
    firewall = SERVER_LIB / "firewall"
    changed = _install_content(
        f"""[Unit]
Description=deadlift's firewall for containers, before Docker starts any (ADR 0001)
Before=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={firewall}

[Install]
WantedBy=multi-user.target
RequiredBy=docker.service""",
        FIREWALL_UNIT,
        0o644,
    )
    # Only after a change, so running bootstrap again leaves systemd alone.
    if changed:
        _must(["systemctl", "daemon-reload"])
    # Applied now as well as by the unit, which runs it only when it starts, so a changed
    # firewall takes effect without restarting anything.
    _must([str(firewall)])
    start_and_enable("deadlift-firewall.service")
    _say("The firewall for containers is in place")


def _docker_remaps() -> bool:
    """Whether the running Docker daemon remaps users."""
    info = run(
        ["docker", "info", "--format", "{{json .SecurityOptions}}"],
        env=_env(),
        stdout=Stream.CAPTURE,
        stderr=Stream.DEVNULL,
    )
    return "name=userns" in info.stdout


def start_docker() -> None:
    start_and_enable("docker.service")
    if _docker_remaps():
        _say("Docker is running with userns-remap")
        return
    _say(
        "Restarting Docker to turn on userns-remap. Its containers stop, and it keeps images and"
        " volumes made before apart from the remapped ones."
    )
    _must(["systemctl", "restart", "docker.service"])
    for _ in range(60):
        if _docker_remaps():
            _say("Docker is running with userns-remap")
            return
        time.sleep(1)
    fail("Docker didn't come back with userns-remap after restarting. See `journalctl -u docker`")


def setup_state() -> None:
    _install_dir(STATE, 0o700)
    _install_dir(STATE / "apps", 0o700)
    settings = STATE / "settings"
    if not settings.is_file():
        _install_content(SETTINGS_HEADER, settings, 0o600)
    for setting in missing_settings(settings.read_text()):
        with settings.open("a") as file:
            file.write(f"{setting}\n")
        _say(f"Set {setting} in {settings}")


def setup_watchdog() -> None:
    """The watchdog, `deploy watchdog`, run by a timer every 5 minutes: over the upper disk
    threshold, it stops every Feature Environment on the Server."""
    service = _install_content(
        f"""[Unit]
Description=deadlift's disk watchdog: stops every Feature Environment when the disk is too full
Wants=docker.service
After=docker.service

[Service]
Type=oneshot
ExecStart={SERVER_LIB / "deploy"} watchdog""",
        WATCHDOG_UNIT.with_name(f"{WATCHDOG_UNIT.name}.service"),
        0o644,
    )
    timer = _install_content(
        """[Unit]
Description=Runs deadlift's disk watchdog every 5 minutes

[Timer]
OnCalendar=*:0/5

[Install]
WantedBy=timers.target""",
        WATCHDOG_UNIT.with_name(f"{WATCHDOG_UNIT.name}.timer"),
        0o644,
    )
    # Only after a change, so running bootstrap again leaves systemd alone.
    if service or timer:
        _must(["systemctl", "daemon-reload"])
    start_and_enable("deadlift-watchdog.timer")
    _say("The watchdog runs every 5 minutes")


def _required(payload: Path) -> list[Path]:
    """What bootstrap needs from ``payload``, relative to it."""
    needed = [Path("deadlift/__init__.py")]
    needed += [Path(*launcher.module.split(".")).with_suffix(".py") for launcher in LAUNCHERS]
    return needed


def remove_retired() -> None:
    """Removes what earlier versions installed and deadlift no longer uses. Last, once the
    units name the launchers by their new paths, so nothing runs an old one in between."""
    for path in remove_retired_launchers(SERVER_LIB):
        _say(f"Removed {path}, which deadlift no longer uses")


def _main(argv: Sequence[str]) -> None:
    os.umask(0o022)
    if os.geteuid() != 0:
        _refuse("bootstrap needs root")
    if len(argv) != 1:
        _refuse("bootstrap takes the directory holding what `deadlift server bootstrap` sends")
    payload = Path(argv[0])
    for needed in _required(payload):
        if not os.access(payload / needed, os.R_OK):
            _refuse(
                f"{needed} isn't beside bootstrap. Run it through `deadlift server bootstrap`,"
                " which sends all of deadlift's Server code"
            )
    if _release().get("ID") != "ubuntu":
        _refuse("a Server runs Ubuntu 24.04, and this isn't Ubuntu")

    # Bootstrap restarts Docker the first time, so it waits for any operation on the Server
    # first.
    _install_dir(STATE, 0o700)
    with server_lock(STATE):
        apt = _Apt()
        _step("Installing what deadlift needs")
        install_prerequisites(apt)
        _step("Configuring Docker")
        configure_docker()
        _step("Installing Docker")
        install_docker(apt)
        _step("Installing Tailscale")
        install_tailscale(apt)
        _step("Installing the entry command and the deploy script")
        install_scripts(payload)
        _step("Installing the firewall for containers")
        setup_firewall()
        _step("Starting Docker")
        start_docker()
        _step(f"Setting up {STATE} and the Server-wide settings")
        setup_state()
        _step("Installing the watchdog's timer")
        setup_watchdog()
        remove_retired()

    _say("deadlift: this machine is a Server, bootstrapped and up to date.")


def main(argv: Sequence[str]) -> int:
    """Bootstraps this machine from ``argv[0]``, the directory `deadlift server bootstrap` sent."""
    return run_main(lambda: _main(argv))
