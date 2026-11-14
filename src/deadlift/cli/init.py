"""``deadlift init``: writes deadlift's starter workflows (cli/starters) into the App's repository,
at .github/workflows/deadlift-{feature,dev,sweep,rollback}.yml.

Usage:
  deadlift init <app> --server <server> [--branch <branch>] [--image <name>]...

  <app>     The App, as `deadlift app add` named it.
  --server  The Server's name on the tailnet.
  --branch  The branch the Dev Environment tracks, main by default.
  --image   An image the workflows build, by its name, once per image: web by default. Each is
            built from the repository's root and its Dockerfile, which the build step of
            deadlift-feature and deadlift-dev can change.

The workflows are the App's own from then on. Each starts with a line holding a hash of what
deadlift init wrote, so a run again rewrites only the ones nobody has changed since, and keeps
the rest as they are.

Every reason line starts with `deadlift: `. Exit codes: 0 done, 1 failed, 2 refused or bad
usage (nothing was written).
"""

import hashlib
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from deadlift.cli.common import ROOT, Usage, is_server_name, maintainer_env, say
from deadlift.cli.common import run_command as _run_command
from deadlift.core.names import image_name_problem, is_app_name
from deadlift.core.process import Stream, run

USAGE = "Usage: deadlift init <app> --server <server> [--branch <branch>] [--image <name>]...\n"
STARTERS = ROOT / "cli" / "starters"
NAMES = ("deadlift-feature", "deadlift-dev", "deadlift-sweep", "deadlift-rollback")

_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_IMAGES_LINE = re.compile(r"( *)@images@")
_WRITTEN = re.compile(rb"# .* Starter sha256:([0-9a-f]{64})")


def usage(why: str) -> NoReturn:
    raise Usage(f"{why}. Nothing was written.", USAGE)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def render(starter: str, *, app: str, server: str, branch: str, images: Sequence[str]) -> str:
    """A starter with this App's values in place of its @placeholders@, as Bash's ``$(…)`` took
    it: without trailing line breaks."""
    lines = starter.split("\n")
    if lines[-1] == "":
        lines.pop()
    out: list[str] = []
    for line in lines:
        found = _IMAGES_LINE.fullmatch(line)
        if found:
            out += [f"{found.group(1)}{image}" for image in images]
            continue
        out.append(
            line.replace("@app@", app)
            .replace("@server@", server)
            .replace("@branch@", branch)
            .replace("@image-names@", " ".join(images))
        )
    return "\n".join(out).rstrip("\n")


def header(digest: str) -> str:
    """The header's 3 lines, holding the hash of the rest of the file as deadlift init wrote it."""
    return (
        "# Written by `deadlift init`, and the App's own: once this file changes, deadlift init\n"
        f"# leaves it alone. Starter sha256:{digest}\n"
        "#\n"
    )


def workflow(body: str) -> bytes:
    """The whole file for a rendered starter: the header, then the body."""
    data = body.encode("utf-8", errors="surrogateescape")
    return header(sha256(data + b"\n")).encode() + data + b"\n"


def written_hash(current: bytes) -> bytes | None:
    """The hash in a file's header, of the rest as deadlift init wrote it: None when it has none."""
    lines = current.split(b"\n")
    if len(lines) < 2:
        return None
    found = _WRITTEN.fullmatch(lines[1])
    return found.group(1) if found else None


def after_header(current: bytes) -> bytes:
    """A file's lines after its 3-line header, as ``tail -n +4`` gives them."""
    parts = current.split(b"\n", 3)
    return parts[3] if len(parts) == 4 else b""


def _cwd() -> str:
    # $PWD, as Bash shows it: the path it was reached by, when that's still where we are.
    logical = os.environ.get("PWD", "")
    try:
        if logical and Path(logical).is_absolute() and Path(logical).samefile("."):
            return logical
    except OSError:
        pass
    return str(Path.cwd())


def init(argv: Sequence[str]) -> None:
    app = argv[0] if argv else ""
    if not app or app.startswith("-"):
        usage("init needs the <app> first")
    rest = list(argv[1:])
    # The same rules as `deadlift app add`.
    if not is_app_name(app):
        usage(
            f"'{app}' isn't an App name, as `deadlift app add` takes them: up to 23 of a-z, 0-9"
            " and -"
        )
    server, branch = "", "main"
    images: list[str] = []
    while rest:
        arg = rest[0]
        if arg not in ("--server", "--branch", "--image"):
            usage(f"unknown argument '{arg}'")
        if len(rest) < 2:
            usage(f"{arg} needs a value")
        value = rest[1]
        if arg == "--server":
            server = value
        elif arg == "--branch":
            branch = value
        else:
            images.append(value)
        del rest[:2]
    if not server:
        usage("init needs --server, the Server's name on the tailnet")
    if not is_server_name(server):
        usage(f"'{server}' isn't a host name")
    env = maintainer_env()
    # Plain names only, which are safe unquoted in YAML.
    if (
        not _BRANCH.fullmatch(branch)
        or not run(
            ["git", "check-ref-format", "--branch", branch],
            env=env,
            stdout=Stream.DEVNULL,
            stderr=Stream.DEVNULL,
        ).ok
    ):
        usage(f"'{branch}' isn't a branch name deadlift init takes: a-z, A-Z, 0-9, ., _, / and -")
    images = images or ["web"]
    for image in images:
        problem = image_name_problem(image)
        if problem is not None:
            usage(problem)

    toplevel = run(
        ["git", "rev-parse", "--show-toplevel"],
        env=env,
        stdout=Stream.CAPTURE,
        stderr=Stream.DEVNULL,
    )
    if not toplevel.ok:
        usage(f"deadlift init writes into the App's git repository, and {_cwd()} isn't in one")
    repo = Path(toplevel.stdout.rstrip("\n"))

    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    for name in NAMES:
        file = workflows / f"{name}.yml"
        shown = f".github/workflows/{name}.yml"
        starter = (STARTERS / f"{name}.yml").read_bytes().decode("utf-8", errors="surrogateescape")
        new = workflow(render(starter, app=app, server=server, branch=branch, images=images))
        if file.exists():
            # What deadlift init wrote last, by the hash in its header, and what the file holds now.
            try:
                current = file.read_bytes()
            except OSError:
                current = b""
            written = written_hash(current)
            if written is None:
                say(f"Kept {shown}, which deadlift init didn't write")
                continue
            if sha256(after_header(current)).encode() != written:
                say(f"Kept {shown}, which has changed since deadlift init wrote it")
                continue
            if current.rstrip(b"\n") == new.rstrip(b"\n"):
                say(f"Unchanged {shown}")
                continue
            verb = "Updated"
        else:
            verb = "Wrote"
        aside = file.with_name(f"{file.name}.deadlift-init")
        aside.write_bytes(new)
        aside.replace(file)
        say(f"{verb} {shown}")

    example = f"${{DEADLIFT_IMAGE_{images[0].upper()}}}"
    say(f"""
Next:
- Commit the workflows. They're the App's own, to change as it likes.
- Set the repository variables TS_OAUTH_CLIENT_ID and TS_AUDIENCE to the Client ID and Audience
  of the federated credential that `deadlift app add` printed, if they aren't set yet.
- The workflows deliver compose.yaml, at the repository's root, as the App's Compose template.
  Each image reaches it as ${{DEADLIFT_IMAGE_<NAME>}}, such as {example}.
- The skip-deploy label on a pull request stops its Feature Environment being deployed.""")


def main(argv: Sequence[str]) -> int:
    return _run_command(lambda: init(argv))
