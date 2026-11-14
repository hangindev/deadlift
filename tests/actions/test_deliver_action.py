"""The deliver action, run through its action.yml on a fake runner. Its `ssh` stands in for the
Server, so these tests check what the action sends and how it reports the Server's answer; the
delivery tests check what the Server does with a delivery."""

import re
from collections.abc import Callable

import pytest

from tests.support.composite_action import ActionRun
from tests.support.fake_runner import FakeRunner, SentDelivery
from tests.support.matchers import Containing

TEMPLATE = "services:\n  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n"


class Runner:
    """A runner whose checkout holds the App's compose.yaml, and a stand-in for the tailnet
    join."""

    def __init__(self, fake: FakeRunner) -> None:
        self.fake = fake
        (fake.workspace / "compose.yaml").write_text(TEMPLATE)
        self.joins: list[dict[str, str]] = []

    def deliver(self, inputs: dict[str, str], *, join_status: int = 0) -> ActionRun:
        def join(with_: dict[str, str]) -> int:
            self.joins.append(with_)
            return join_status

        return self.fake.run_action(
            "actions/deliver",
            {
                "app": "shop",
                "server": "deadlift-1",
                "oauth-client-id": "client-123",
                "audience": "api.tailscale.com/client-123",
                **inputs,
            },
            uses={"tailscale/github-action": join},
        )


@pytest.fixture
def runner(make_runner: Callable[[], FakeRunner]) -> Runner:
    return Runner(make_runner())


DEPLOYED = "\n".join(
    [
        "deadlift-output: environment=feature-42",
        "deadlift-output: url=https://shop.tailtest.ts.net:10001/",
        "deadlift: deployed feature-42, which is up at https://shop.tailtest.ts.net:10001/",
        "",
    ]
)


def test_joins_the_tailnet_as_the_apps_ci_delivers_as_its_deploy_user_and_leaves(
    runner: Runner,
) -> None:
    runner.fake.server_answers(DEPLOYED)

    run = runner.deliver(
        {
            "operation": "deploy",
            "environment": "feature-42",
            "images": (
                "web=ghcr.io/acme/shop/web:sha-1234\nworker=ghcr.io/acme/shop/worker:sha-1234\n"
            ),
        }
    )

    assert run.status == 0, run.stderr
    assert runner.joins == [
        {
            "oauth-client-id": "client-123",
            "audience": "api.tailscale.com/client-123",
            "tags": "tag:deadlift-ci-shop",
            "ping": "deadlift-1",
        }
    ]
    assert runner.fake.deliveries() == [
        SentDelivery(
            destination="deadlift-shop@deadlift-1",
            command=[
                "sudo",
                "--non-interactive",
                "/usr/local/sbin/deadlift-entry",
                "--protocol",
                "1",
                "deploy",
                "feature-42",
                "--image",
                "web=ghcr.io/acme/shop/web:sha-1234",
                "--image",
                "worker=ghcr.io/acme/shop/worker:sha-1234",
            ],
            files={"compose.yaml": TEMPLATE, "token": "fake-job-token"},
        )
    ]
    assert "sudo tailscale logout" in runner.fake.calls()
    assert run.outputs == {
        "outcome": "done",
        "reason": "deadlift: deployed feature-42, which is up at https://shop.tailtest.ts.net:10001/",
        "url": "https://shop.tailtest.ts.net:10001/",
        "environment": "feature-42",
        "swept": "",
    }


def operation_sent(runner: Runner) -> list[str]:
    """The words of the operation the Server was asked to run, after the entry command's own."""
    delivery = runner.fake.deliveries()[0]
    return delivery.command[delivery.command.index("--protocol") + 2 :]


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        pytest.param(
            {"operation": "sweep", "keep": "42, 57\n61\n"},
            ["sweep", "--keep", "42,57,61"],
            id="a Sweep keeping some keys",
        ),
        pytest.param(
            {"operation": "sweep", "keep-none": "true"},
            ["sweep", "--keep-none"],
            id="a Sweep keeping none",
        ),
        pytest.param(
            {
                "operation": "deploy",
                "environment": "feature-42",
                "images": "web=x:1",
                "keep": "42,57",
            },
            ["deploy", "feature-42", "--image", "web=x:1", "--keep", "42,57"],
            id="a Feature deploy that sweeps first",
        ),
        pytest.param(
            {"operation": "destroy", "environment": "feature-42"},
            ["destroy", "feature-42"],
            id="a destroy",
        ),
        pytest.param(
            {"operation": "rollback", "environment": "dev"},
            ["rollback", "dev"],
            id="a Rollback",
        ),
        pytest.param(
            {"operation": "rollback", "environment": "dev", "discard-newer-data": "true"},
            ["rollback", "dev", "--discard-newer-data"],
            id="a Rollback that may discard newer data",
        ),
    ],
)
def test_delivers(runner: Runner, inputs: dict[str, str], expected: list[str]) -> None:
    run = runner.deliver(inputs)

    assert run.status == 0, run.stderr
    assert operation_sent(runner) == expected


def test_sends_the_template_only_with_a_deploy(runner: Runner) -> None:
    runner.deliver({"operation": "destroy", "environment": "feature-42"})

    assert list(runner.fake.deliveries()[0].files) == ["token"]


@pytest.mark.parametrize("input_", ["keep-none", "discard-newer-data"])
def test_refuses_a_flag_that_isnt_true_or_false(runner: Runner, input_: str) -> None:
    run = runner.deliver({"operation": "sweep", input_: "yes"})

    assert run.status == 2
    assert re.search(f"^deadlift: {input_} must be true or false, not 'yes'", run.stderr, re.M)
    assert runner.fake.deliveries() == []


def test_explains_the_delivery_in_the_job_summary(runner: Runner) -> None:
    runner.fake.server_answers(f"deadlift-output: swept=7,9\n{DEPLOYED}")

    run = runner.deliver(
        {"operation": "deploy", "environment": "feature-42", "images": "web=x:1", "keep": "42"}
    )

    assert "deadlift deploy feature-42: done" in run.summary
    assert (
        "deadlift: deployed feature-42, which is up at https://shop.tailtest.ts.net:10001/"
        in run.summary
    )
    assert re.search(r"Address: https://shop\.tailtest\.ts\.net:10001/", run.summary)
    assert re.search(r"Swept: 7, 9", run.summary)


def test_explains_a_failure_in_the_job_summary_even_when_the_server_gave_no_reason(
    runner: Runner,
) -> None:
    runner.fake.server_answers("Killed\n", 1)

    run = runner.deliver({"operation": "destroy", "environment": "feature-42"})

    assert "deadlift destroy feature-42: failed" in run.summary
    assert re.search(r"gave no reason", run.summary)


def test_passes_a_skipped_delivery_saying_why(runner: Runner) -> None:
    reason = "deadlift: not deployed, because no Slot is free: feature-7, feature-9 hold them all"
    runner.fake.server_answers(f"{reason}\n", 75)

    run = runner.deliver({"operation": "deploy", "environment": "feature-42", "images": "web=x:1"})

    assert run.status == 0, run.stderr
    assert run.outputs == Containing({"outcome": "skipped", "reason": reason})


@pytest.mark.parametrize(
    "status", [pytest.param(1, id="failed"), pytest.param(2, id="was refused")]
)
def test_fails_the_step_when_the_delivery_fails_saying_why_and_still_leaves(
    runner: Runner, status: int
) -> None:
    reason = "deadlift: feature-42 didn't become healthy, so it's stopped"
    runner.fake.server_answers(f"{reason}\n", status)

    run = runner.deliver({"operation": "deploy", "environment": "feature-42", "images": "web=x:1"})

    assert run.status == status
    assert run.outputs == Containing({"outcome": "failed", "reason": reason})
    assert "sudo tailscale logout" in runner.fake.calls()


def test_fails_without_delivering_when_it_cant_join_the_tailnet_and_still_leaves(
    runner: Runner,
) -> None:
    run = runner.deliver({"operation": "destroy", "environment": "feature-42"}, join_status=1)

    assert run.status != 0
    assert runner.fake.deliveries() == []
    assert "sudo tailscale logout" in runner.fake.calls()
