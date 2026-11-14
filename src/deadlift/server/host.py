"""What the Server's code asks of the host, shared by the deploy script and `deadlift app`: the
Compose project of each Environment, how full the disk is, and the tailnet's name.
"""

import json
import re
from pathlib import Path
from typing import cast

from deadlift.core.process import Env, Stream, Target, run
from deadlift.server.layout import layout
from deadlift.server.text import json_text, lines

_PERCENT = re.compile(r"100|[1-9]?[0-9]")


def project_name(app: str, environment: str) -> str:
    """The Compose project of ``app``'s ``environment`` (dev or feature-<key>). The App-name
    rule keeps one App's projects from ever being another's."""
    return f"deadlift-{app}-{environment}"


def by_project(project: str) -> list[str]:
    """`docker ... ls` arguments for what Compose labelled as ``project``'s."""
    return ["--filter", f"label=com.docker.compose.project={project}"]


def docker_lines(env: Env, *args: str) -> list[str]:
    """The lines ``docker <args>`` prints, as Bash's ``mapfile -t`` reads them (none when it
    prints nothing)."""
    done = run(["docker", *args], env=env, stdout=Stream.CAPTURE)
    return lines(done.stdout)


def remove_project(project: str, env: Env) -> bool:
    """Removes every container, volume and network of the Compose project, by Compose's own
    label, so it needs neither the template nor its variables. Returns whether it all went."""
    for kind, remove in (
        ("container", ["container", "rm", "--force", "--volumes"]),
        ("volume", ["volume", "rm"]),
        ("network", ["network", "rm"]),
    ):
        listing = ["container", "ls", "--all"] if kind == "container" else [kind, "ls"]
        ids = docker_lines(env, *listing, "--quiet", *by_project(project))
        if ids and not run(["docker", *remove, *ids], env=env, stdout=Stream.DEVNULL).ok:
            return False
    return True


def _numeric_sort_key(text: str) -> tuple[float, str]:
    """``sort -n``'s order: by the leading number, then as text."""
    matched = re.match(r"\s*(-?[0-9]*\.?[0-9]+|-?[0-9]+\.?)", text)
    return (float(matched[1]) if matched else 0.0, text)


def fullest_disk(df: str) -> str:
    """The highest percentage used in ``df -P``'s output, as
    ``awk 'NR > 1 { sub(/%$/, "", $5); print $5 }' | sort -n | tail -n 1`` gives it."""
    values = [
        fields[4].removesuffix("%") if len(fields) >= 5 else ""
        for fields in (line.split() for line in lines(df)[1:])
    ]
    return max(values, key=_numeric_sort_key) if values else ""


def disk_used(state: Path, env: Env) -> int | None:
    """How full the disk is, as a whole percentage: the fuller of the filesystems holding the
    Server's state and Docker's data. The tests' layout replaces the measure. None when it
    can't be measured."""
    stand_in = layout().disk_used
    if stand_in is not None:
        try:
            text = stand_in.read_text().strip()
        except OSError:
            return None
        return int(text) if _PERCENT.fullmatch(text) else None
    paths = [str(state)]
    info = run(
        ["docker", "info", "--format", "{{.DockerRootDir}}"],
        env=env,
        stdout=Stream.CAPTURE,
        stderr=Stream.DEVNULL,
    )
    root = info.stdout.rstrip("\n")
    if root and Path(root).is_dir():
        paths.append(root)
    # POSIX output: the fifth column is the percentage used.
    df = run(["df", "-P", *paths], env=env, stdout=Stream.CAPTURE)
    used = fullest_disk(df.stdout)
    return int(used) if re.fullmatch(r"[0-9]+", used) else None


def magic_dns_suffix(env: Env, *, errors: Target = Stream.INHERIT) -> str:
    """The tailnet's MagicDNS suffix, as `tailscale status` gives it, for the Apps' addresses on
    their Services, with what Tailscale says when it can't going to ``errors``. "" when
    Tailscale doesn't say."""
    done = run(["tailscale", "status", "--json"], env=env, stdout=Stream.CAPTURE, stderr=errors)
    if not done.ok:
        return ""
    try:
        status: object = json.loads(done.stdout)
    except ValueError:
        return ""
    if not isinstance(status, dict):
        return ""
    value = cast(dict[str, object], status).get("MagicDNSSuffix")
    if value is None or value is False:
        return ""
    return json_text(value).rstrip("\n")
