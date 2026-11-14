"""deadlift: the maintainer's command, run on their own computer, through bin/deadlift.

Usage:
  deadlift server provision
  deadlift server bootstrap <server>
  deadlift server status <server>
  deadlift app add <app> --server <server> --repo <owner/name> [--slots <n>] [--snapshots <n>]
    [--memory <size>] [--cpus <n>] [--pids <n>]
  deadlift app secrets <app> dev|feature <file> --server <server>
  deadlift app remove <app> --server <server> --yes
  deadlift init <app> --server <server> [--branch <branch>] [--image <name>]...

  server provision  A wizard that walks you through getting a new Server, step by step: the
                    tailnet policy, buying the machine, the provider's firewall, bootstrap,
                    joining the tailnet and closing public SSH. Stop it any time and run it
                    again to carry on (see deadlift.cli.provision).
  server bootstrap  Turns <server>, a fresh Ubuntu 24.04 machine or a Server already, into an up
                    to date Server, over SSH as root (see deadlift.cli.server_bootstrap). Safe to
                    run again: a run with nothing to do changes nothing. <server> is anything
                    `ssh root@` takes: the machine's public address before it joins the tailnet,
                    its tailnet name after.
  server status     Shows the Server's disk usage, and each App's ports, Slots in use, Volume
                    Snapshots and Environments.
  app add           Gives the App its place on the Server: its deploy user, deadlift-<app>,
                    outside the docker group, one sudo rule for the entry command, a block of
                    100 loopback ports and its settings (deadlift.server.app has the defaults).
                    Then creates its Tailscale Service, svc:<app>, tagged tag:deadlift-svc and
                    listing 443 and every Slot's port, 10000 + <slot>. Last, it prints the
                    App's own tailnet policy entries and the federated credential its CI joins
                    the tailnet with. The Service needs no entry: the provision wizard's cover
                    every Service tagged tag:deadlift-svc. Safe to run again, and running it
                    again with other settings changes them, keeping the App's ports.
                      --repo       The App's GitHub repository, whose workflows deliver it.
                      --slots      Feature Environments at once, 1 to 9.
                      --snapshots  Volume Snapshots of the Dev Environment to keep.
                      --memory, --cpus, --pids
                                   The most memory (such as 512m), CPUs (such as 0.5) and
                                   processes each of the App's containers may use.
  app secrets       Puts <file>, an env file, on the Server as the App's secrets file for its
                    Dev or its Feature Environments. Its variables reach the template from the
                    next deploy of that kind on. Secrets never pass through CI.
  app remove        Removes the App from the Server: every Environment with its volumes, its
                    Volume Snapshots and secrets files, its deploy user and sudo rule, and its
                    Tailscale Service. Its data is gone for good, hence --yes.
  init              Writes the starter workflows into the App's repository, the one it's run
                    in: deadlift-feature, deadlift-dev, deadlift-sweep and deadlift-rollback,
                    delivering the App to <server>, with Dev tracking <branch> (main by
                    default) and building each --image (web by default). They're the
                    App's own from then on: running it again leaves any it has changed alone
                    (see deadlift.cli.init).

app add and app remove reach the Tailscale API with the maintainer's own credential (see
deadlift.cli.app). The Server never holds a Tailscale credential (ADR 0003).

Every reason line starts with `deadlift: `. Exit codes: 0 done, 1 failed, 2 refused or bad
usage (nothing was changed).
"""

from collections.abc import Sequence

from deadlift.cli import app, init, provision, server_bootstrap
from deadlift.cli.common import run_command, usage


def _dispatch(argv: Sequence[str]) -> int | None:
    first = argv[0] if argv else ""
    second = argv[1] if len(argv) > 1 else ""
    # Matched as Bash matched "${1:-} ${2:-}".
    command = f"{first} {second}"
    match command:
        case "server provision":
            if len(argv) != 2:
                usage("server provision takes no arguments: it asks for what it needs")
            return provision.main([])
        case "server bootstrap":
            return server_bootstrap.main(argv[2:])
        case "server status":
            app.server_status(argv[2:])
        case "app add":
            app.app_add(argv[2:])
        case "app secrets":
            app.app_secrets(argv[2:])
        case "app remove":
            app.app_remove(argv[2:])
        case _ if command.startswith("init "):
            return init.main(argv[1:])
        case _:
            usage(f"'{' '.join(argv)}' isn't a command")
    return None


def main(argv: Sequence[str]) -> int:
    return run_command(lambda: _dispatch(argv))
