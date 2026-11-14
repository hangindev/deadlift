"""What the delivery tests on a bootstrapped Server use to tell what an App's containers can
reach: the same TCP probe from the host's side and, as `PROBE`, from inside a container."""

import inspect
import json
from typing import cast


def reach(host: str, port: int) -> str:
    """What connecting gives: "reached", "timeout" (dropped) or the error, as "ECONNREFUSED"."""
    # Imported here, so that PROBE, which carries this function's source, stands alone.
    import errno
    import socket

    try:
        with socket.create_connection((host, port), timeout=3):
            return "reached"
    except TimeoutError:
        return "timeout"
    except OSError as error:
        return errno.errorcode.get(error.errno or 0, "error")


# The same, for `python3 -c PROBE '<targets as JSON>'` inside a container: every target at once,
# then their results, in order, as a JSON list.
PROBE = (
    inspect.getsource(reach)
    + """
import json
import sys
from concurrent.futures import ThreadPoolExecutor

targets = json.loads(sys.argv[1])
with ThreadPoolExecutor(max(len(targets), 1)) as pool:
    print(json.dumps(list(pool.map(lambda target: reach(target["host"], target["port"]), targets))))
"""
)


def external_ipv4_addresses(ip_addr_json: str) -> list[str]:
    """The host's own IPv4 addresses but its loopback's, from `ip -json -4 address show`."""
    interfaces = cast(list[dict[str, object]], json.loads(ip_addr_json))
    return [
        cast(str, address["local"])
        for interface in interfaces
        if "LOOPBACK" not in cast(list[str], interface.get("flags", []))
        for address in cast(list[dict[str, object]], interface.get("addr_info", []))
    ]
