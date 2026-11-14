"""The server-setup tests' shared setup: the maintainer, reaching this machine over SSH as root.

These tests bootstrap the machine they run on into a Server, so they refuse to start anywhere but
a throwaway machine (tests/support/throwaway.py). A plain `uv run pytest` leaves this directory
out (pyproject.toml's norecursedirs); CI runs it by path.
"""

import secrets
import shutil
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from tests.server_setup.maintainer import REPO, Maintainer
from tests.support.commands import ok
from tests.support.throwaway import why_not_throwaway


@pytest.fixture(scope="module", autouse=True)
def maintainer() -> Generator[Maintainer]:
    """Per test file: refuses a machine that isn't a throwaway one, then gives the maintainer
    an SSH key of their own that root here accepts."""
    why = why_not_throwaway()
    if why:
        pytest.fail(f"Refusing to run the server-setup tests: {why}.", pytrace=False)
    hex_ = secrets.token_hex(4)
    work = Path(tempfile.mkdtemp(prefix=f"deadlift-test-{hex_}-"))
    try:
        ok("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", work / "key")
        ok("sudo", "bash", REPO / "test/server-setup/prepare.sh", work / "key.pub")
        (work / "bin").mkdir()
        ssh = work / "bin/ssh"
        ssh.write_text(
            f"#!/bin/sh\nexec /usr/bin/ssh -i {work / 'key'} -o IdentitiesOnly=yes"
            f' -o UserKnownHostsFile={work / "known_hosts"} "$@"\n'
        )
        ssh.chmod(0o755)
        yield Maintainer(work, hex_)
    finally:
        shutil.rmtree(work, ignore_errors=True)
