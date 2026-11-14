"""The build action, run through its action.yml on a fake runner whose `docker` stands in for
Docker and GHCR, recording what was pushed."""

import re
from collections.abc import Callable
from typing import Protocol

import pytest

from tests.support.composite_action import ActionRun
from tests.support.fake_runner import FakeRunner, Login

COMMIT = "0123456789abcdef0123456789abcdef01234567"


class Runner:
    """A runner whose checkout holds these files (Dockerfiles, by path)."""

    def __init__(self, fake: FakeRunner, files: list[str]) -> None:
        self.fake = fake
        for file in files:
            path = fake.workspace / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("FROM scratch\n")

    def build(self, inputs: dict[str, str]) -> ActionRun:
        return self.fake.run_action(
            "actions/build",
            inputs,
            github={
                "sha": COMMIT,
                "repository": "Acme/Shop",
                "actor": "octocat",
                "server_url": "https://github.com",
            },
        )


class MakeRunner(Protocol):
    def __call__(self, files: list[str] | None = None) -> Runner: ...


@pytest.fixture
def runner(make_runner: Callable[[], FakeRunner]) -> MakeRunner:
    def make(files: list[str] | None = None) -> Runner:
        return Runner(make_runner(), ["Dockerfile"] if files is None else files)

    return make


def test_pushes_each_named_image_to_ghcr_as_sha_commit_and_outputs_their_references_for_deliver(
    runner: MakeRunner,
) -> None:
    fake = runner(["Dockerfile", "worker/Dockerfile"])

    run = fake.build({"images": "web\nworker context=worker\n"})

    assert run.status == 0, run.stderr
    # GHCR's names are lowercase.
    assert [image.ref for image in fake.fake.pushed()] == [
        f"ghcr.io/acme/shop/web:sha-{COMMIT}",
        f"ghcr.io/acme/shop/worker:sha-{COMMIT}",
    ]
    assert run.outputs["images"] == (
        f"web=ghcr.io/acme/shop/web:sha-{COMMIT}\nworker=ghcr.io/acme/shop/worker:sha-{COMMIT}"
    )


def test_pushes_one_image_for_the_servers_platform_with_no_attestations_which_prune_registry_needs(
    runner: MakeRunner,
) -> None:
    fake = runner()

    fake.build({"images": "web"})
    fake.build({"images": "web", "platform": "linux/arm64"})

    assert [(i.platform, i.provenance, i.sbom) for i in fake.fake.pushed()] == [
        ("linux/amd64", "false", "false"),
        ("linux/arm64", "false", "false"),
    ]


def test_pushes_with_the_jobs_token_and_leaves_no_login_behind_on_the_runner(
    runner: MakeRunner,
) -> None:
    fake = runner()

    run = fake.build({"images": "web"})

    assert run.status == 0, run.stderr
    assert fake.fake.pushed()[0].login == Login(
        registry="ghcr.io",
        username="octocat",
        password="fake-job-token",  # noqa: S106
    )
    assert not (fake.fake.home / ".docker/config.json").exists()


def test_labels_each_image_with_its_repository_and_commit_so_ghcr_links_the_package_to_the_repository(  # noqa: E501
    runner: MakeRunner,
) -> None:
    fake = runner()

    fake.build({"images": "web"})

    labels = fake.fake.pushed()[0].labels
    assert "org.opencontainers.image.source=https://github.com/Acme/Shop" in labels
    assert f"org.opencontainers.image.revision={COMMIT}" in labels


def test_builds_an_image_from_the_dockerfile_context_and_stage_it_names(
    runner: MakeRunner,
) -> None:
    fake = runner(["Dockerfile", "docker/sandbox.Dockerfile"])

    run = fake.build(
        {"images": "web\n\nsandbox_runner file=docker/sandbox.Dockerfile target=runtime\n"}
    )

    assert run.status == 0, run.stderr
    assert [(i.ref, i.context, i.dockerfile, i.target) for i in fake.fake.pushed()] == [
        (f"ghcr.io/acme/shop/web:sha-{COMMIT}", ".", "./Dockerfile", ""),
        (
            f"ghcr.io/acme/shop/sandbox_runner:sha-{COMMIT}",
            ".",
            "docker/sandbox.Dockerfile",
            "runtime",
        ),
    ]


@pytest.mark.parametrize(
    ("inputs", "reason"),
    [
        pytest.param(
            {"images": "Web"},
            r"'Web' isn't an image name",
            id="an image name deliver couldn't pass on",
        ),
        pytest.param(
            {"images": "web_"}, r"'web_' isn't an image name", id="an image name GHCR can't hold"
        ),
        pytest.param(
            {"images": "web\nweb context=web"},
            r"the image web is given twice",
            id="the same image twice",
        ),
        pytest.param(
            {"images": "web dockerfile=x"},
            r"'dockerfile=x' isn't a setting of the image web",
            id="an unknown setting",
        ),
        pytest.param({"images": "\n"}, r"no image was named", id="no image"),
        pytest.param(
            {"images": "web", "platform": "linux/amd64,linux/arm64"},
            r"--platform needs one platform",
            id="several platforms",
        ),
        pytest.param(
            {"images": "web", "commit": "main"},
            r"--commit needs a full commit SHA",
            id="a commit that isn't a full SHA",
        ),
    ],
)
def test_refuses_building_nothing(runner: MakeRunner, inputs: dict[str, str], reason: str) -> None:
    fake = runner()

    run = fake.build(inputs)

    assert run.status == 2
    assert re.search(f"^deadlift: {reason}.*, so nothing was built\\.$", run.stderr, re.M)
    assert fake.fake.pushed() == []


def test_fails_when_an_image_doesnt_build_saying_which_and_what_was_pushed_before_it(
    runner: MakeRunner,
) -> None:
    fake = runner(["Dockerfile"])

    run = fake.build({"images": "web\nworker context=worker\nsandbox"})

    assert run.status == 1
    assert re.search(
        "^deadlift: couldn't build and push the image worker\\. "
        f"Pushed before it: ghcr\\.io/acme/shop/web:sha-{COMMIT}\\.$",
        run.stderr,
        re.M,
    )
    assert [image.ref for image in fake.fake.pushed()] == [f"ghcr.io/acme/shop/web:sha-{COMMIT}"]
    assert run.outputs["images"] == ""


def test_lists_what_it_pushed_in_the_job_summary(runner: MakeRunner) -> None:
    fake = runner()

    run = fake.build({"images": "web"})

    assert f"ghcr.io/acme/shop/web:sha-{COMMIT}" in run.summary
