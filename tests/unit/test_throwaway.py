"""The guard that keeps the server-setup tests, and the delivery tests that need a bootstrapped
Server, off any machine that isn't thrown away after the run."""

import pytest

from tests.support.throwaway import why_not_throwaway

RUNNER = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "ImageOS": "ubuntu24"}


def test_lets_a_github_hosted_ubuntu_24_04_runner_through() -> None:
    assert why_not_throwaway(RUNNER, marker_exists=False) is None


def test_lets_a_multipass_vm_marked_by_multipass_sh_through() -> None:
    assert why_not_throwaway({}, marker_exists=True) is None


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({}, id="a developer's machine"),
        pytest.param({**RUNNER, "RUNNER_ENVIRONMENT": "self-hosted"}, id="a self-hosted runner"),
        pytest.param(
            {**RUNNER, "ImageOS": "ubuntu22"}, id="a GitHub-hosted runner of another image"
        ),
        pytest.param(
            {"GITHUB_ACTIONS": "true"}, id="a machine that only claims to be in GitHub Actions"
        ),
    ],
)
def test_stops(env: dict[str, str]) -> None:
    why = why_not_throwaway(env, marker_exists=False)
    assert why is not None
    assert "isn't a throwaway machine" in why
