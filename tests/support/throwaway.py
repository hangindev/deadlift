"""Whether this machine may be reconfigured by tests: bootstrapped as a Server, its Docker restarted
with userns-remap, its firewall and network changed. Only a machine thrown away after the run
may: a GitHub-hosted Ubuntu 24.04 runner, or a Multipass VM that tests/server_setup/multipass.sh
launched and marked. Never a developer's machine, even one with passwordless sudo."""

import os
from collections.abc import Mapping
from pathlib import Path

THROWAWAY_MARKER = Path("/etc/deadlift-throwaway-machine")
"""The file tests/server_setup/multipass.sh creates in the VMs it launches for the server-setup
tests."""


def why_not_throwaway(
    env: Mapping[str, str] | None = None, *, marker_exists: bool | None = None
) -> str | None:
    """Why this machine isn't a throwaway one, or None when it is."""
    if env is None:
        env = os.environ
    if marker_exists is None:
        marker_exists = THROWAWAY_MARKER.exists()
    github_hosted = (
        env.get("GITHUB_ACTIONS") == "true"
        and env.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and env.get("ImageOS") == "ubuntu24"
    )
    if github_hosted or marker_exists:
        return None
    return (
        "this isn't a throwaway machine (a GitHub-hosted ubuntu-24.04 runner, or a Multipass VM "
        f"marked with {THROWAWAY_MARKER} by tests/server_setup/multipass.sh), and these tests "
        "reconfigure the machine's Docker, firewall and network"
    )
