"""Fixtures for the CLI tests: deadlift run on a maintainer's computer."""

from collections.abc import Iterator

import pytest

from tests.support.app_repository import AppRepository, app_repository_at
from tests.support.provision_world import World, world_at


@pytest.fixture
def app_repository() -> Iterator[AppRepository]:
    """A fresh App repository, removed after the test."""
    with app_repository_at() as repo:
        yield repo


@pytest.fixture
def world() -> Iterator[World]:
    """A maintainer's computer and the fake machines and tailnet around it, removed after."""
    with world_at() as w:
        yield w
