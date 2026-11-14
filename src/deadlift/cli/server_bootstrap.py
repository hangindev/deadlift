"""``deadlift server bootstrap <server>``: turns <server>, a fresh Ubuntu 24.04 machine or a Server
already, into an up to date Server. It sends this checkout's server/ over SSH as root and runs its
bootstrap.sh there (see it for what it sets up). Safe to run again: a run with nothing to do
changes nothing. <server> is anything ``ssh root@`` takes: the machine's public address before it
joins the tailnet, its tailnet name after.

Interim: this is bin/deadlift's Bash sender as it was, ported as is so ``deadlift`` keeps working
while the Server's half of bootstrap moves to Python. The server-setup port replaces this module
with the sender of ``deadlift.bootstrap`` (ADR 0004), keeping ``main(argv)``.
"""

from collections.abc import Sequence

from deadlift.cli.common import (
    ROOT,
    is_server_name,
    maintainer_env,
    run_command,
    say,
    shell_status,
    usage,
)
from deadlift.core.outcome import fail
from deadlift.core.process import Stream, run, start

# Run on the Server in a private directory of root's, with the tar of server/ on stdin.
REMOTE = (
    'd=$(mktemp -d) && tar -xf - -C "$d" --no-same-owner && "$d/bootstrap.sh"; s=$?;'
    ' rm -rf "$d"; exit $s'
)


def bootstrap(argv: Sequence[str]) -> int:
    if len(argv) != 1:
        usage("server bootstrap needs one <server>")
    server = argv[0]
    if not is_server_name(server):
        usage(f"'{server}' isn't a host name or address")
    say(f"Bootstrapping {server}")
    # COPYFILE_DISABLE keeps macOS tar from adding ._ files. Commands on the Server read nothing
    # from the maintainer's terminal: stdin is the tar.
    env = maintainer_env()
    tar = start(
        ["tar", "-C", str(ROOT / "server"), "-cf", "-", "."],
        env=env.with_vars({"COPYFILE_DISABLE": "1"}),
        stdout=Stream.CAPTURE,
    )
    if tar.stdout is None:  # pragma: no cover (CAPTURE gives a pipe)
        raise RuntimeError("tar has no stdout pipe")
    try:
        ssh = run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", f"root@{server}", REMOTE],
            env=env,
            stdin=tar.stdout,
            stdout=Stream.INHERIT,
        )
    finally:
        tar.stdout.close()
    tar_code = shell_status(tar.wait())
    # As Bash's pipefail: ssh's status when it failed, else tar's.
    status = shell_status(ssh.code) if ssh.code != 0 else tar_code
    if status == 255:
        fail(f"couldn't reach {server} over SSH as root, so nothing was changed.")
    return status


def main(argv: Sequence[str]) -> int:
    return run_command(lambda: bootstrap(argv))
