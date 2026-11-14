#!/usr/bin/env bash
# Stand-ins for what a Server's containers must not reach, for tests/delivery/test_isolation.py on a
# throwaway machine only (it changes the machine's network), as root:
#
#   outside.sh up <name> <port>    set them up, each answering TCP on <port>
#   outside.sh down <name>         remove them
#
# <name> is short (up to 8 of a-z0-9), as it names network interfaces. Set up:
#   - A machine beyond the host, a network namespace reached over a veth pair, holding an address
#     in each range a container mustn't reach: the tailnet (100.80.213.2), the cloud metadata
#     address (169.254.169.254, routed there for the test's length) and each private range
#     (10.213.0.2, 172.16.213.2, 192.168.213.2). Without deadlift's firewall, a container reaches
#     all of them through the host, as it would a tailnet peer or a neighbour in the data centre.
#   - An address of the host's own in the tailnet's range (100.80.214.1), on a dummy interface,
#     as tailscale0's is on a Server.
set -euo pipefail

action=${1:?outside.sh up|down <name> [port]}
name=${2:?outside.sh needs a name}
[[ $name =~ ^[a-z0-9]{1,8}$ ]] || { echo "outside.sh: bad name $name" >&2 && exit 2; }
ns=$name-ns host_if=$name-h peer_if=$name-p tailnet_if=$name-t
beyond=(100.80.213.2 169.254.169.254 10.213.0.2 172.16.213.2 192.168.213.2)

down() {
  if ip netns list | grep -qw "$ns"; then
    ip netns pids "$ns" | xargs -r kill
    ip netns delete "$ns"
  fi
  local address
  for address in "${beyond[@]}"; do ip route delete "$address/32" 2>/dev/null || true; done
  ip link delete "$host_if" 2>/dev/null || true
  ip link delete "$tailnet_if" 2>/dev/null || true
}

up() {
  local port=${1:?outside.sh up needs a port} address
  down
  ip netns add "$ns"
  ip link add "$host_if" type veth peer name "$peer_if"
  ip link set "$peer_if" netns "$ns"
  ip address add 198.18.213.1/30 dev "$host_if"
  ip link set "$host_if" up
  ip -n "$ns" address add 198.18.213.2/30 dev "$peer_if"
  ip -n "$ns" link set "$peer_if" up
  ip -n "$ns" link set lo up
  ip -n "$ns" route add default via 198.18.213.1
  for address in "${beyond[@]}"; do
    ip -n "$ns" address add "$address/32" dev lo
    ip route replace "$address/32" via 198.18.213.2 dev "$host_if"
  done
  ip link add "$tailnet_if" type dummy
  ip address add 100.80.214.1/32 dev "$tailnet_if"
  ip link set "$tailnet_if" up
  # Something answering on every address beyond the host.
  ip netns exec "$ns" setsid python3 -m http.server "$port" --bind 0.0.0.0 \
    </dev/null >/dev/null 2>&1 &
  for _ in $(seq 50); do
    if ip netns exec "$ns" bash -c "</dev/tcp/127.0.0.1/$port" 2>/dev/null; then return; fi
    sleep 0.1
  done
  echo "outside.sh: the listener beyond the host didn't start" >&2
  exit 1
}

case $action in
  up) up "${3:-}" ;;
  down) down ;;
  *) echo "outside.sh: unknown action $action" >&2 && exit 2 ;;
esac
