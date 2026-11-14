"""deadlift's firewall for containers (ADR 0001): a container on a Server can reach the internet,
and nothing of the Server's own, the tailnet's, the cloud's metadata service or any private
network. Bootstrap installs its launcher, owned by root, at /usr/local/lib/deadlift/firewall.sh,
runs it, and installs deadlift-firewall.service, which runs it at every boot before Docker starts.

For IPv4 and IPv6 alike, it keeps two chains of its own in the mangle table:

  DEADLIFT-FORWARD  Jumped to from FORWARD. Drops a container's new connections to the tailnet
                    (100.64.0.0/10, fd7a:115c:a1e0::/48), link-local addresses (169.254.0.0/16,
                    which holds 169.254.169.254, and fe80::/10) and private ranges (10.0.0.0/8,
                    172.16.0.0/12, 192.168.0.0/16, fc00::/7). Traffic between Docker networks
                    is left to Docker, which already keeps them apart.
  DEADLIFT-INPUT    Jumped to from INPUT for packets from a Docker bridge (docker0, br-*). Drops a
                    container's new connections to any of the host's own addresses, so it can't
                    reach the Server's services, Tailscale Serve or another App's ports through
                    the host. Replies to connections the host made, such as Docker publishing
                    the routed service on loopback, still pass.

The rules sit in the mangle table, which is read before the filter table, rather than in Docker's
DOCKER-USER chain: tailscaled puts its own chain at the top of FORWARD when it comes up, and that
chain accepts everything going out to the tailnet, so a rule in DOCKER-USER can come too late.
Neither Docker nor Tailscale touches the mangle table.

It's safe to run again: a chain that already holds exactly these rules is left alone, and a jump
already in place isn't added twice.

Exit codes: 0 done, 1 failed.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from deadlift.core.outcome import fail, run_main
from deadlift.core.process import Stream, run, scrubbed_env

Tool = Literal["iptables", "ip6tables"]

# What a container may never start a connection to, by address family.
DROPPED: dict[Tool, tuple[str, ...]] = {
    "iptables": (
        "10.0.0.0/8",
        "100.64.0.0/10",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
    ),
    "ip6tables": ("fc00::/7", "fe80::/10"),
}
# The interfaces of Docker's bridge networks: the default one, and one per Compose network.
BRIDGES = ("docker0", "br-+")
FORWARD_CHAIN = "DEADLIFT-FORWARD"
INPUT_CHAIN = "DEADLIFT-INPUT"
_REPLIES = "-m conntrack --ctstate RELATED,ESTABLISHED -j RETURN"

_ENV = scrubbed_env()


@dataclass(frozen=True)
class Rules:
    """The rules of deadlift's two chains for one address family, each written as
    ``iptables -S`` prints it, without its ``-A <chain>``."""

    forward: tuple[str, ...]
    input: tuple[str, ...]


def rules(tool: Tool) -> Rules:
    """What deadlift's chains hold in the mangle table of ``tool``."""
    forward = [_REPLIES]
    forward += [f"-o {bridge} -j RETURN" for bridge in BRIDGES]
    forward += [f"-d {network} -j DROP" for network in DROPPED[tool]]
    to_host = [_REPLIES]
    if tool == "ip6tables":
        # Neighbour discovery, without which a container on an IPv6 network can't reach its
        # gateway.
        to_host += [
            f"-p ipv6-icmp -m icmp6 --icmpv6-type {kind} -j RETURN" for kind in (133, 135, 136)
        ]
    to_host.append("-j DROP")
    return Rules(tuple(forward), tuple(to_host))


def chain_holds(listing: str, chain: str, wanted: Sequence[str]) -> bool:
    """Whether ``listing``, what ``iptables -S <chain>`` printed, is exactly ``wanted``."""
    lines = [f"-N {chain}", *(f"-A {chain} {rule}" for rule in wanted)]
    return listing.rstrip("\n") == "\n".join(lines)


def _mangle(tool: Tool, *args: str) -> list[str]:
    return [tool, "-w", "-t", "mangle", *args]


def _must(argv: Sequence[str]) -> None:
    done = run(argv, env=_ENV, stdout=Stream.INHERIT)
    if not done.ok:
        fail(f"the firewall couldn't be set up: {' '.join(argv)} failed with exit code {done.code}")


def _set_chain(tool: Tool, chain: str, wanted: Sequence[str]) -> None:
    """Makes ``chain`` in the mangle table of ``tool`` hold exactly ``wanted``. Changes nothing
    when it does already."""
    exists = run(_mangle(tool, "-S", chain), env=_ENV, stdout=Stream.DEVNULL, stderr=Stream.DEVNULL)
    if not exists.ok:
        _must(_mangle(tool, "-N", chain))
    listing = run(_mangle(tool, "-S", chain), env=_ENV, stdout=Stream.CAPTURE)
    if chain_holds(listing.stdout, chain, wanted):
        return
    _must(_mangle(tool, "-F", chain))
    for rule in wanted:
        _must(_mangle(tool, "-A", chain, *rule.split()))


def _ensure_jump(tool: Tool, chain: str, *rule: str) -> None:
    """Puts ``rule`` at the top of ``chain`` in the mangle table of ``tool``, unless it's there."""
    there = run(_mangle(tool, "-C", chain, *rule), env=_ENV, stderr=Stream.DEVNULL)
    if not there.ok:
        _must(_mangle(tool, "-I", chain, "1", *rule))


def _setup(tool: Tool) -> None:
    wanted = rules(tool)
    _set_chain(tool, FORWARD_CHAIN, wanted.forward)
    _set_chain(tool, INPUT_CHAIN, wanted.input)
    _ensure_jump(tool, "FORWARD", "-j", FORWARD_CHAIN)
    for bridge in BRIDGES:
        _ensure_jump(tool, "INPUT", "-i", bridge, "-j", INPUT_CHAIN)


def _main() -> None:
    if os.geteuid() != 0:
        fail("the firewall needs root, so nothing was changed.")
    _setup("iptables")
    _setup("ip6tables")


def main(argv: Sequence[str]) -> int:
    """Sets the firewall up. Takes no arguments (and, as the Bash did, ignores any)."""
    del argv
    return run_main(_main)
