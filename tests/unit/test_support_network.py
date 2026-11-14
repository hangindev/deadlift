"""What the delivery tests on a bootstrapped Server use to tell what an App's containers reach."""

import json
import socket
import subprocess
import sys

from tests.support.network import PROBE, external_ipv4_addresses, reach

IP_ADDR = json.dumps(
    [
        {"ifname": "lo", "flags": ["LOOPBACK", "UP"], "addr_info": [{"local": "127.0.0.1"}]},
        {
            "ifname": "eth0",
            "flags": ["BROADCAST", "UP"],
            "addr_info": [{"family": "inet", "local": "10.0.0.5"}, {"local": "10.0.0.6"}],
        },
        {
            "ifname": "tailscale0",
            "flags": ["POINTOPOINT"],
            "addr_info": [{"local": "100.80.214.1"}],
        },
        {"ifname": "dummy0", "flags": ["BROADCAST"], "addr_info": []},
    ]
)


def test_the_hosts_external_addresses_are_every_interfaces_but_the_loopbacks() -> None:
    assert external_ipv4_addresses(IP_ADDR) == ["10.0.0.5", "10.0.0.6", "100.80.214.1"]


def test_a_listener_is_reached() -> None:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        port = listener.getsockname()[1]

        assert reach("127.0.0.1", port) == "reached"


def test_a_closed_port_gives_the_connections_error_code() -> None:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        port = listener.getsockname()[1]

    assert reach("127.0.0.1", port) == "ECONNREFUSED"


def test_the_probe_reaches_every_target_at_once_and_prints_the_results_in_order() -> None:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        open_port = listener.getsockname()[1]
        with socket.create_server(("127.0.0.1", 0)) as closing:
            closed_port = closing.getsockname()[1]
        targets = [
            {"host": "127.0.0.1", "port": closed_port},
            {"host": "127.0.0.1", "port": open_port},
        ]

        probed = subprocess.run(
            [sys.executable, "-I", "-c", PROBE, json.dumps(targets)],
            capture_output=True,
            text=True,
            check=True,
        )

    assert json.loads(probed.stdout) == ["ECONNREFUSED", "reached"]
