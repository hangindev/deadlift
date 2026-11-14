"""The delivery tests drive real containers, so they run only when named
(`uv run pytest tests/delivery`), one at a time, and each phase of a test gets minutes, not
seconds."""

from collections.abc import Generator, Iterator

import pytest

from tests.support.harness import Harness, start_harness, time_limit

TIME_LIMIT = 300


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item: pytest.Item) -> Generator[None]:
    with time_limit(TIME_LIMIT):
        return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None]:
    with time_limit(TIME_LIMIT):
        return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item) -> Generator[None]:
    with time_limit(TIME_LIMIT):
        return (yield)


@pytest.fixture(scope="module")
def harness() -> Iterator[Harness]:
    """One run per test file: a local registry and the example App's images in it."""
    run = start_harness()
    try:
        yield run
    finally:
        run.remove()


@pytest.fixture
def remove_servers_after_each(harness: Harness) -> Iterator[None]:
    """Removes the Servers a test made once it's done."""
    yield
    harness.remove_servers()


@pytest.fixture(scope="class")
def remove_servers_after_class(harness: Harness) -> Iterator[None]:
    """Removes the Servers a class's tests made once they're all done."""
    yield
    harness.remove_servers()
