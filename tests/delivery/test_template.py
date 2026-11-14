"""What deadlift requires of an App's Compose template: a routed service it can judge by its
Compose healthcheck (ADR 0002)."""

import re
from collections.abc import Callable

import pytest

from tests.support.harness import Harness, example_template, matches

pytestmark = pytest.mark.usefixtures("remove_servers_after_each")


def with_healthcheck(healthcheck: str) -> str:
    """The example template with its routed service's healthcheck replaced by `healthcheck`."""
    return re.sub(
        r"^ {4}healthcheck:\n(?: {6}.*\n)+",
        lambda _: f"    healthcheck:\n{healthcheck}" if healthcheck else "",
        example_template(),
        count=1,
        flags=re.MULTILINE,
    )


REFUSALS: list[tuple[str, Callable[[], str], str]] = [
    (
        "has no x-deadlift.route",
        lambda: re.sub(r"^x-deadlift:\n.*\n", "", example_template(), count=1, flags=re.MULTILINE),
        r"x-deadlift\.route",
    ),
    (
        "routes to a service it doesn't have",
        lambda: example_template().replace("route: web:3000", "route: website:3000", 1),
        r"website",
    ),
    ("routes to a service with no healthcheck", lambda: with_healthcheck(""), r"no healthcheck"),
    (
        "routes to a service whose healthcheck is disabled",
        lambda: with_healthcheck("      disable: true\n"),
        r"disabled",
    ),
    (
        "routes to a service whose healthcheck test is NONE",
        lambda: with_healthcheck('      test: ["NONE"]\n'),
        r"disabled",
    ),
    (
        "names its route in a shape deadlift doesn't read",
        lambda: example_template().replace("x-deadlift:\n  route: web:3000", "x-deadlift: web", 1),
        r"couldn't be checked",
    ),
]


@pytest.mark.parametrize(
    ("template", "why"),
    [(template, why) for _, template, why in REFUSALS],
    ids=[f"that {name}" for name, _, _ in REFUSALS],
)
def test_a_template_is_refused_and_nothing_runs(
    harness: Harness, template: Callable[[], str], why: str
) -> None:
    server = harness.server()
    app = server.apps["a"].name

    result = server.deliver(
        app, ["deploy", "feature-1", "--image", f"web={harness.image()}"], template=template()
    )

    assert result.code == 2, result.output
    assert result.outcome == "failed"
    assert matches(r"^deadlift: ", result.reason)
    assert matches(why, result.reason)
    assert server.containers(app) == ""
    assert server.serve_mappings() == []
