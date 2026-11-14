"""Fixtures for the actions tests: deadlift's composite actions and starter workflows, run on a
fake runner against a fake GitHub. Nothing reaches the network or Docker.

The one thing they need that a Server doesn't have is jq: the fake `gh` applies `--jq` filters
with the real jq, as the real gh applies them, because the starter workflows' filters are jq."""

from collections.abc import Callable, Iterator
from contextlib import ExitStack

import pytest

from tests.support.app_repository import AppRepository, app_repository_at
from tests.support.fake_github import FakeGitHub, GitHubState
from tests.support.fake_runner import FakeRunner


@pytest.fixture
def make_runner() -> Iterator[Callable[[], FakeRunner]]:
    """Makes fake runners, each removed after the test."""
    made: list[FakeRunner] = []

    def make() -> FakeRunner:
        made.append(FakeRunner())
        return made[-1]

    yield make
    for runner in made:
        runner.cleanup()


@pytest.fixture
def make_github() -> Iterator[Callable[[GitHubState], FakeGitHub]]:
    """Makes fake GitHubs from a state, each removed after the test."""
    made: list[FakeGitHub] = []

    def make(state: GitHubState) -> FakeGitHub:
        made.append(FakeGitHub(state))
        return made[-1]

    yield make
    for github in made:
        github.cleanup()


@pytest.fixture
def make_app_repository() -> Iterator[Callable[[], AppRepository]]:
    """Makes fresh App repositories, each removed after the test."""
    with ExitStack() as stack:
        yield lambda: stack.enter_context(app_repository_at())
