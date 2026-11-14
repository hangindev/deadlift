"""`deadlift server bootstrap`: what it sends a Server."""

import io
import tarfile
from pathlib import Path

import pytest

from deadlift.cli import server_bootstrap

CHECKOUT = Path(__file__).resolve().parents[2]


def sent_names(checkout: Path) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(server_bootstrap.payload(checkout))) as tar:
        return {member.name for member in tar.getmembers() if member.isfile()}


def test_sends_bootstrap_with_the_servers_python() -> None:
    names = sent_names(CHECKOUT)

    assert {
        "deadlift/__init__.py",
        "deadlift/core/process.py",
        "deadlift/bootstrap/bootstrap.py",
        "deadlift/bootstrap/install.py",
        "deadlift/server/app.py",
        "deadlift/server/firewall.py",
    } <= names


def test_sends_nothing_a_server_doesnt_run(tmp_path: Path) -> None:
    for module in ("core/x.py", "server/x.py", "bootstrap/x.py", "cli/x.py", "actions/x.py"):
        (tmp_path / "src/deadlift" / module).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "src/deadlift" / module).write_text("")
    (tmp_path / "src/deadlift/__init__.py").write_text("")
    (tmp_path / "src/deadlift/server/__pycache__").mkdir()
    (tmp_path / "src/deadlift/server/__pycache__/x.cpython-312.pyc").write_text("")
    (tmp_path / "src/deadlift/server/notes.txt").write_text("")

    assert sent_names(tmp_path) == {
        "deadlift/__init__.py",
        "deadlift/core/x.py",
        "deadlift/server/x.py",
        "deadlift/bootstrap/x.py",
    }


def test_sends_files_owned_by_no_one_in_particular() -> None:
    with tarfile.open(fileobj=io.BytesIO(server_bootstrap.payload(CHECKOUT))) as tar:
        owners = {(member.uid, member.gid, member.uname, member.gname) for member in tar}

    assert owners == {(0, 0, "", "")}


@pytest.mark.parametrize(
    "server", ["localhost", "203.0.113.7", "my-server.tail1.ts.net", "fd7a::1"]
)
def test_takes_a_host_name_or_address(server: str) -> None:
    assert server_bootstrap.server_problem(server) is None


@pytest.mark.parametrize("server", ["", "-oProxyCommand=x", "root@host", "a b", "host\n", "::1"])
def test_refuses_what_isnt_a_host_name_or_address(server: str) -> None:
    assert server_bootstrap.server_problem(server) == f"'{server}' isn't a host name or address"
