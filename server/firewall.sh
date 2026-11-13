#!/usr/bin/env bash
# deadlift's firewall for containers (ADR 0001): a container on a Server can reach the internet,
# and nothing of the Server's own, the tailnet's, the cloud's metadata service or any private
# network. Bootstrap installs it, owned by root, at /usr/local/lib/deadlift/firewall.sh, runs it,
# and installs deadlift-firewall.service, which runs it at every boot before Docker starts.
#
# For IPv4 and IPv6 alike, it keeps two chains of its own in the mangle table:
#
#   DEADLIFT-FORWARD  Jumped to from FORWARD. Drops a container's new connections to the tailnet
#                     (100.64.0.0/10, fd7a:115c:a1e0::/48), link-local addresses (169.254.0.0/16,
#                     which holds 169.254.169.254, and fe80::/10) and private ranges (10.0.0.0/8,
#                     172.16.0.0/12, 192.168.0.0/16, fc00::/7). Traffic between Docker networks
#                     is left to Docker, which already keeps them apart.
#   DEADLIFT-INPUT    Jumped to from INPUT for packets from a Docker bridge (docker0, br-*). Drops a
#                     container's new connections to any of the host's own addresses, so it can't
#                     reach the Server's services, Tailscale Serve or another App's ports through
#                     the host. Replies to connections the host made, such as Docker publishing
#                     the routed service on loopback, still pass.
#
# The rules sit in the mangle table, which is read before the filter table, rather than in
# Docker's DOCKER-USER chain: tailscaled puts its own chain at the top of FORWARD when it comes up,
# and that chain accepts everything going out to the tailnet, so a rule in DOCKER-USER can come too
# late. Neither Docker nor Tailscale touches the mangle table.
#
# It's safe to run again: a chain that already holds exactly these rules is left alone, and a jump
# already in place isn't added twice.
#
# Exit codes: 0 done, 1 failed.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "deadlift: the firewall needs root, so nothing was changed." >&2
  exit 1
fi

# What a container may never start a connection to, by address family.
ipv4_dropped=(10.0.0.0/8 100.64.0.0/10 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16)
ipv6_dropped=(fc00::/7 fe80::/10)
# The interfaces of Docker's bridge networks: the default one, and one per Compose network.
bridges=(docker0 br-+)

# Makes chain $2 in the mangle table of $1 (iptables or ip6tables) hold exactly the rules after it,
# each written as `iptables -S` prints it. Changes nothing when it does already.
set_chain() {
  local tool=$1 chain=$2 rule
  shift 2
  local want=("-N $chain")
  for rule; do want+=("-A $chain $rule"); done
  if ! "$tool" -w -t mangle -S "$chain" >/dev/null 2>&1; then
    "$tool" -w -t mangle -N "$chain"
  fi
  if [[ $("$tool" -w -t mangle -S "$chain") == "$(printf '%s\n' "${want[@]}")" ]]; then return; fi
  "$tool" -w -t mangle -F "$chain"
  for rule; do
    local words
    read -ra words <<<"$rule"
    "$tool" -w -t mangle -A "$chain" "${words[@]}"
  done
}

# Puts the rule after $2 at the top of chain $2 in the mangle table of $1, unless it's there.
ensure_jump() {
  local tool=$1 chain=$2
  shift 2
  if ! "$tool" -w -t mangle -C "$chain" "$@" 2>/dev/null; then
    "$tool" -w -t mangle -I "$chain" 1 "$@"
  fi
}

setup() {
  local tool=$1 range bridge type forward=() input=()
  shift
  forward=("-m conntrack --ctstate RELATED,ESTABLISHED -j RETURN")
  for bridge in "${bridges[@]}"; do forward+=("-o $bridge -j RETURN"); done
  for range; do forward+=("-d $range -j DROP"); done
  input=("-m conntrack --ctstate RELATED,ESTABLISHED -j RETURN")
  if [[ $tool == ip6tables ]]; then
    # Neighbour discovery, without which a container on an IPv6 network can't reach its gateway.
    for type in 133 135 136; do input+=("-p ipv6-icmp -m icmp6 --icmpv6-type $type -j RETURN"); done
  fi
  input+=("-j DROP")

  set_chain "$tool" DEADLIFT-FORWARD "${forward[@]}"
  set_chain "$tool" DEADLIFT-INPUT "${input[@]}"
  ensure_jump "$tool" FORWARD -j DEADLIFT-FORWARD
  for bridge in "${bridges[@]}"; do ensure_jump "$tool" INPUT -i "$bridge" -j DEADLIFT-INPUT; done
}

setup iptables "${ipv4_dropped[@]}"
setup ip6tables "${ipv6_dropped[@]}"
