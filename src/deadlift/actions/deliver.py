"""deliver: the client half of a delivery, run by the ``deliver`` action on the App's CI job once it
has joined the tailnet. It sends the delivery over SSH as the App's deploy user to the Server's
entry command, and reports what happened. The delivery tests drive it end to end.

Usage:
  deliver.py --server <host> --app <app> [--compose <file>] <operation> [arguments...]
  deliver.py --from-action-inputs

  --server   The Server's name on the tailnet.
  --app      The App, whose deploy user deadlift-<app> the delivery logs in as.
  --compose  The App's Compose template, sent with ``deploy`` only. compose.yaml by default.
  The operation and its arguments are passed to the entry command as given, for example
  ``deploy feature-42 --image web=ghcr.io/owner/app/web:sha-1234``.
  --from-action-inputs  Takes all of these from the deliver action's inputs instead, as
             INPUT_<NAME> (see :func:`action_argv`).

Environment:
  DEADLIFT_REGISTRY_TOKEN  The job's registry token, which the Server pulls the App's images
                           with. Sent inside the delivery, never on a command line, and removed
                           on the Server after the operation.
  GITHUB_OUTPUT            Where the step's outputs go, when set:
                             outcome      done, skipped or failed
                             reason       the last line starting ``deadlift: ``
                             url          the Environment's address, when it has one
                             environment  the Environment acted on
                             swept        the keys of Feature Environments the Sweep destroyed
  GITHUB_STEP_SUMMARY      Where the job summary goes, when set: the operation, its outcome and
                           reason, and the URL and swept keys when there are any.

The delivery is one tar on the entry command's stdin, holding only compose.yaml and the token.
The Server runs its own installed deploy script, never anything delivered.

Exit codes: 0 for done and skipped, so a skip doesn't fail the step. Otherwise the entry
command's (1 failed, 2 refused), or 1 when the Server couldn't be reached.
"""

import os
import shlex
import shutil
import stat
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from deadlift.actions import job
from deadlift.core.outcome import REASON_PREFIX, parse_outputs, reason, run_main
from deadlift.core.process import NOT_FOUND, Stream, shell_status, start

USAGE = "deliver.py --server <host> --app <app> [--compose <file>] <operation> [arguments...]"

PROTOCOL = 1
"""The delivery protocol this client speaks. The entry command refuses one it doesn't support,
saying to re-run bootstrap."""

ENTRY_COMMAND = "/usr/local/sbin/deadlift-entry"

TOKEN_VARIABLE = "DEADLIFT_REGISTRY_TOKEN"  # noqa: S105 - its name, not its value
"""Where the job's registry token comes from, to be sent inside the delivery and nowhere else."""

# ssh's own exit code when it couldn't connect or log in.
_SSH_FAILED = 255


@dataclass(frozen=True)
class Report:
    """What a delivery came to: its outcome, the Server's last reason line, its outputs, and
    the exit code the step ends with."""

    outcome: str
    reason: str
    url: str = ""
    environment: str = ""
    swept: str = ""
    status: int = 0


def remote_command(words: Sequence[str]) -> str:
    """The command line the deploy user's shell on the Server runs: the entry command through
    sudo, with ``words`` (the operation and its arguments) quoted to reach it as they are."""
    argv = ["sudo", "--non-interactive", ENTRY_COMMAND, "--protocol", str(PROTOCOL), *words]
    return " ".join(shlex.quote(word) for word in argv)


def report(status: int, log: str, *, server: str, app: str) -> Report:
    """What a delivery came to, from ssh's exit code and everything the Server printed. Its
    outputs are lines of their own, ``deadlift-output: <name>=<value>``; the last of each counts,
    as does the last reason line."""
    lines = log.split("\n")
    outputs = parse_outputs(lines)
    said = [line for line in lines if line.startswith(REASON_PREFIX)]
    last = said[-1] if said else ""
    found = {name: outputs.get(name, "") for name in ("url", "environment", "swept")}
    match status:
        case 0:
            return Report("done", last, **found, status=0)
        case 75:
            return Report("skipped", last, **found, status=0)
        case _ if status == _SSH_FAILED:
            unreachable = (
                f"{REASON_PREFIX}couldn't reach {server} over SSH as deadlift-{app},"
                " so nothing was run."
            )
            return Report("failed", unreachable, **found, status=1)
        case _:
            return Report("failed", last, **found, status=status)


def summary(operation: str, arguments: Sequence[str], delivered: Report) -> list[str]:
    """The job summary's lines for a delivery: the operation (and the Environment, when its
    first argument names one), its outcome and reason, and its address and what was swept."""
    title = operation
    if arguments and arguments[0] and not arguments[0].startswith("-"):
        title += f" {arguments[0]}"
    said = delivered.reason or "The Server gave no reason. The step's log has the details."
    lines = [f"### deadlift {title}: {delivered.outcome}", "", f"> {said}"]
    if delivered.url or delivered.swept:
        lines.append("")
    if delivered.url:
        lines.append(f"- Address: {delivered.url}")
    if delivered.swept:
        lines.append(f"- Swept: {delivered.swept.replace(',', ', ')}")
    return lines


def _copy_like_cp(source: Path, target: Path) -> None:
    """Copies a file as ``cp`` does: its contents, and its mode less the umask."""
    shutil.copyfile(source, target)
    umask = os.umask(0)
    os.umask(umask)
    target.chmod(stat.S_IMODE(source.stat().st_mode) & ~umask)


def _send(work: Path, members: list[str], destination: str, command: str) -> tuple[int, str]:
    """Streams the delivery tar into ssh, echoing what the Server prints (its stdout and stderr
    together) to stdout as it comes. Returns ssh's exit code and what it printed."""
    # The token goes only inside the delivery: neither tar nor ssh needs it.
    env = job.job_env(drop=(TOKEN_VARIABLE,))
    tar_argv = ["tar", "-C", str(work / "delivery"), "-cf", "-", "--files-from=/dev/null"]
    ssh_argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30"]
    ssh_argv += ["-o", "StrictHostKeyChecking=accept-new", destination, command]
    read_end, write_end = os.pipe()
    with os.fdopen(read_end, "rb", buffering=0) as server_said:
        try:
            tar = start([*tar_argv, *members], env=env, stdout=Stream.CAPTURE)
            delivery = tar.stdout or Stream.DEVNULL
            try:
                ssh = start(ssh_argv, env=env, stdin=delivery, stdout=write_end, stderr=write_end)
            finally:
                if tar.stdout is not None:
                    tar.stdout.close()
        except FileNotFoundError as error:
            # As a shell says it, into the log, and with its exit code.
            missing = f"{error.filename}: command not found\n"
            sys.stdout.write(missing)
            sys.stdout.flush()
            return NOT_FOUND, missing
        finally:
            os.close(write_end)
        log = bytearray()
        while chunk := server_said.read(65536):
            log += chunk
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    status = shell_status(ssh.wait())
    tar.wait()
    return status, log.decode("utf-8", errors="surrogateescape")


def action_argv() -> list[str]:
    """The command line the deliver action's inputs make: its operation and Environment, each
    image (one per line or separated by spaces), the keys to keep (separated by commas, spaces
    or newlines) and its flags, which must each be true or false."""
    keep_none = job.flag_input("keep-none", what="nothing was delivered")
    discard = job.flag_input("discard-newer-data", what="nothing was delivered")
    args = ["--server", job.action_input("server"), "--app", job.action_input("app")]
    args += ["--compose", job.action_input("compose"), job.action_input("operation")]
    if environment := job.action_input("environment"):
        args.append(environment)
    for image in job.words(job.action_input("images")):
        args += ["--image", image]
    if keep := job.words(job.action_input("keep").replace(",", " ")):
        args += ["--keep", ",".join(keep)]
    if keep_none:
        args.append("--keep-none")
    if discard:
        args.append("--discard-newer-data")
    return args


def _deliver(argv: Sequence[str]) -> int:
    if list(argv) == [job.FROM_INPUTS]:
        argv = action_argv()
    options = {"server": "", "app": "", "compose": "compose.yaml"}
    args = list(argv)
    while args and args[0].startswith("-"):
        arg = args.pop(0)
        if arg[2:] not in options or not arg.startswith("--"):
            job.usage(f"unknown option {arg}", USAGE)
        if not args or not args[0]:
            job.usage(f"{arg} needs a value", USAGE)
        options[arg[2:]] = args.pop(0)
    server, app, compose = options["server"], options["app"], options["compose"]
    if not server:
        job.usage("--server is required", USAGE)
    if not app:
        job.usage("--app is required", USAGE)
    if not args:
        job.usage("no operation given", USAGE)
    operation, arguments = args[0], args[1:]

    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        (work / "delivery").mkdir()
        members: list[str] = []
        if operation == "deploy":
            if not Path(compose).is_file():
                job.usage(f"there's no Compose template at {compose}", USAGE)
            _copy_like_cp(Path(compose), work / "delivery" / "compose.yaml")
            members.append("compose.yaml")
        token = os.environ.get(TOKEN_VARIABLE, "")
        if token:
            fd = os.open(work / "delivery" / "token", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as file:
                file.write(token)
            members.append("token")
        # Always a tar, even one holding nothing, as for an operation that needs no template or
        # token.
        status, log = _send(work, members, f"deadlift-{app}@{server}", remote_command(args))

    delivered = report(status, log, server=server, app=app)
    if status == _SSH_FAILED:
        reason(delivered.reason.removeprefix(REASON_PREFIX))
    job.append(
        "GITHUB_OUTPUT",
        [
            f"outcome={delivered.outcome}",
            f"reason={delivered.reason}",
            f"url={delivered.url}",
            f"environment={delivered.environment}",
            f"swept={delivered.swept}",
        ],
    )
    job.append("GITHUB_STEP_SUMMARY", summary(operation, arguments, delivered))
    return delivered.status


def main(argv: Sequence[str]) -> int:
    return run_main(lambda: _deliver(argv))
