"""Fixtures for the CLI tests: deadlift run on a maintainer's computer."""

from collections.abc import Iterator

import pytest

from tests.support.app_repository import AppRepository, app_repository_at


@pytest.fixture
def app_repository() -> Iterator[AppRepository]:
    """A fresh App repository, removed after the test."""
    with app_repository_at() as repo:
        yield repo
