"""The prune-registry action, driven through its script with a fake GitHub on PATH."""

import itertools
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import pytest

from tests.support.composite_action import ActionRun
from tests.support.fake_github import Failure, FakeGitHub, GitHubState, PackageVersion, Run

SCRIPT = "actions/prune-registry/prune-registry.py"

_ids = itertools.count(1)


def version(tags: list[str], age_in_days: int = 0) -> PackageVersion:
    """A GHCR version pushed `age_in_days` ago with these tags."""
    id_ = next(_ids)
    created = datetime(2026, 11, 13, tzinfo=UTC) - timedelta(days=age_in_days)
    return {
        "id": id_,
        "name": f"sha256:{id_:064x}",
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "metadata": {"package_type": "container", "container": {"tags": tags}},
    }


class Registry:
    """A fake GitHub where acme/shop's images are in GHCR as acme's packages shop/<image>."""

    def __init__(self, fake: FakeGitHub) -> None:
        self.fake = fake

    def prune(self, *args: str) -> Run:
        return self.fake.run(SCRIPT, args, {"GITHUB_REPOSITORY": "acme/shop"})

    def tags(self, image: str) -> list[str]:
        """The tags of each version of an image still in GHCR."""
        versions = self.fake.state().get("packages", {})["acme"][f"shop/{image}"]
        return sorted(",".join(v["metadata"]["container"]["tags"]) for v in versions)


class MakeRegistry(Protocol):
    def __call__(
        self,
        images: dict[str, list[PackageVersion]],
        *,
        owner_type: Literal["User", "Organization"] = "Organization",
        failures: list[Failure] | None = None,
        max_per_page: int = 100,
    ) -> Registry: ...


@pytest.fixture
def github(make_github: Callable[[GitHubState], FakeGitHub]) -> MakeRegistry:
    def make(
        images: dict[str, list[PackageVersion]],
        *,
        owner_type: Literal["User", "Organization"] = "Organization",
        failures: list[Failure] | None = None,
        max_per_page: int = 100,
    ) -> Registry:
        return Registry(
            make_github(
                {
                    "repos": {
                        "acme/shop": {"ownerType": owner_type, "branches": {}, "commits": {}}
                    },
                    "owners": {"acme": owner_type},
                    "packages": {"acme": {f"shop/{image}": v for image, v in images.items()}},
                    "failures": failures or [],
                    "maxPerPage": max_per_page,
                }
            )
        )

    return make


def test_deletes_an_images_versions_except_the_ones_its_told_to_keep(github: MakeRegistry) -> None:
    fake = github({"web": [version(["sha-c1"], 3), version(["sha-c2"], 2), version(["sha-c3"], 1)]})

    run = fake.prune("--keep", "sha-c2,sha-c3", "web")

    assert run.status == 0, run.stderr
    assert fake.tags("web") == ["sha-c2", "sha-c3"]


def test_prunes_images_of_a_repository_a_user_owns(github: MakeRegistry) -> None:
    fake = github({"web": [version(["sha-c1"], 2), version(["sha-c2"], 1)]}, owner_type="User")

    run = fake.prune("--keep", "sha-c2", "web")

    assert run.status == 0, run.stderr
    assert fake.tags("web") == ["sha-c2"]


def test_prunes_every_image_its_given_however_many_pages_of_versions_each_has(
    github: MakeRegistry,
) -> None:
    def many(prefix: str) -> list[PackageVersion]:
        return [version([f"{prefix}-{i}"], i) for i in range(7)]

    fake = github(
        {"web": many("sha-w"), "worker": many("sha-k"), "sandbox": many("sha-s")}, max_per_page=3
    )

    run = fake.prune("--keep", "sha-w-6,sha-k-6", "web", "worker")

    assert run.status == 0, run.stderr
    assert fake.tags("web") == ["sha-w-6"]
    assert fake.tags("worker") == ["sha-k-6"]
    assert len(fake.tags("sandbox")) == 7


def test_keeps_a_version_by_any_of_its_tags_or_by_its_digest_and_deletes_untagged_ones(
    github: MakeRegistry,
) -> None:
    by_digest = version(["sha-c1"], 4)
    fake = github(
        {
            "web": [
                by_digest,
                version([], 3),
                version(["sha-c2", "nightly"], 2),
                version(["sha-c3"], 1),
            ]
        }
    )

    run = fake.prune("--keep", f"nightly {by_digest['name']}", "web")

    assert run.status == 0, run.stderr
    assert fake.tags("web") == ["sha-c1", "sha-c2,nightly"]


# A workflow that builds its keep list from a failed API call passes an empty one.
@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["--keep", ""], id='["--keep",""]'),
        pytest.param(["--keep", " , "], id='["--keep"," , "]'),
        pytest.param([], id="[]"),
    ],
)
def test_refuses_an_empty_keep_list_deleting_nothing(github: MakeRegistry, args: list[str]) -> None:
    fake = github({"web": [version(["sha-c1"], 2), version(["sha-c2"], 1)]})

    run = fake.prune(*args, "web")

    assert run.status == 2
    assert re.search(r"^deadlift: .*keep-none", run.stderr, re.M)
    assert fake.tags("web") == ["sha-c1", "sha-c2"]


# Such as bare commits where the tags are sha-<commit>: every version would go.
def test_refuses_a_keep_list_that_names_none_of_an_images_versions_deleting_nothing(
    github: MakeRegistry,
) -> None:
    fake = github(
        {
            "web": [version(["sha-c1"], 2), version(["sha-c2"], 1)],
            "worker": [version(["sha-c1"], 2), version(["sha-c2"], 1)],
            "sandbox": [version(["sha-c2"], 1)],
        }
    )

    run = fake.prune("--keep", "c2,sha-c1", "web", "worker", "sandbox")

    assert run.status == 2
    assert re.search(r"^deadlift: .*none of sandbox's 1 versions", run.stderr, re.M)
    assert fake.tags("web") == ["sha-c1", "sha-c2"]
    assert fake.tags("worker") == ["sha-c1", "sha-c2"]


def test_fails_deleting_nothing_when_an_images_versions_cant_be_listed(
    github: MakeRegistry,
) -> None:
    fake = github(
        {
            "web": [version(["sha-c1"], 2), version(["sha-c2"], 1)],
            "worker": [version(["sha-c2"], 1)],
        },
        failures=[
            {
                "method": "GET",
                "pathPrefix": "orgs/acme/packages/container/shop%2Fworker/",
                "status": 403,
            }
        ],
    )

    run = fake.prune("--keep", "sha-c2", "web", "worker")

    assert run.status == 1
    assert re.search(
        r"^deadlift: couldn't list worker's versions .*nothing was deleted", run.stderr, re.M
    )
    assert fake.tags("web") == ["sha-c1", "sha-c2"]


def test_fails_deleting_nothing_when_github_cant_say_who_owns_the_repository(
    github: MakeRegistry,
) -> None:
    fake = github(
        {"web": [version(["sha-c1"], 2), version(["sha-c2"], 1)]},
        failures=[{"method": "GET", "pathPrefix": "repos/acme/shop"}],
    )

    run = fake.prune("--keep", "sha-c2", "web")

    assert run.status == 1
    assert re.search(
        r"^deadlift: couldn't look up acme/shop .*nothing was deleted", run.stderr, re.M
    )
    assert fake.tags("web") == ["sha-c1", "sha-c2"]


# Such as a new App's, before its first build pushed anything, which the starter Sweep prunes.
def test_has_nothing_to_delete_for_an_app_with_no_images_in_ghcr_yet(github: MakeRegistry) -> None:
    fake = github({})

    run = fake.prune("--keep", "sha-c2", "web", "worker")

    assert run.status == 0, run.stderr
    assert re.search(
        r"^deadlift: deleted 0 of 0 versions of web, worker\. web and worker aren't in GHCR",
        run.stderr,
        re.M,
    )


def test_has_nothing_to_delete_for_an_image_with_no_versions(github: MakeRegistry) -> None:
    fake = github({"web": []})

    run = fake.prune("--keep", "sha-c2", "web")

    assert run.status == 0, run.stderr
    assert re.search(r"^deadlift: deleted 0 of 0 versions of web\.$", run.stderr, re.M)


def test_prunes_the_images_in_ghcr_and_names_the_ones_that_arent(github: MakeRegistry) -> None:
    fake = github({"web": [version(["sha-c1"], 2), version(["sha-c2"], 1)]})

    run = fake.prune("--keep", "sha-c2", "web", "wrker")

    assert run.status == 0, run.stderr
    assert re.search(
        r"^deadlift: deleted 1 of 2 versions of web, wrker\. wrker isn't in GHCR .*can't see it",
        run.stderr,
        re.M,
    )
    assert fake.tags("web") == ["sha-c2"]


def test_fails_saying_what_it_deleted_when_a_deletion_fails(github: MakeRegistry) -> None:
    fake = github(
        {
            "web": [version(["sha-c1"], 3), version(["sha-c2"], 2), version(["sha-c3"], 1)],
            "worker": [version(["sha-c1"], 3), version(["sha-c3"], 1)],
        },
        failures=[
            {"method": "DELETE", "pathPrefix": "orgs/acme/packages/container/shop%2Fworker/"}
        ],
    )

    run = fake.prune("--keep", "sha-c3", "web", "worker")

    assert run.status == 1
    assert re.search(
        r"^deadlift: couldn't delete worker:sha-c1\b.*2 versions were deleted before it",
        run.stderr,
        re.M,
    )
    assert fake.tags("web") == ["sha-c3"]
    assert fake.tags("worker") == ["sha-c1", "sha-c3"]


def test_reports_what_it_deleted(github: MakeRegistry) -> None:
    untagged = version([], 3)
    fake = github(
        {
            "web": [untagged, version(["sha-c2"], 2), version(["sha-c3"], 1)],
            "worker": [version(["sha-c3"], 1)],
        }
    )

    run = fake.prune("--keep", "sha-c3", "web", "worker")

    assert run.status == 0, run.stderr
    assert "Deleted web:sha-c2\n" in run.stdout
    assert f"Deleted web@{untagged['name']}\n" in run.stdout
    assert re.search(r"^deadlift: deleted 2 of 4 versions of web, worker\.$", run.stderr, re.M)


def test_says_what_it_would_delete_and_deletes_nothing_on_a_dry_run(github: MakeRegistry) -> None:
    fake = github({"web": [version(["sha-c1"], 2), version(["sha-c2"], 1)]})

    run = fake.prune("--dry-run", "--keep", "sha-c2", "web")

    assert run.status == 0, run.stderr
    assert "Would delete web:sha-c1\n" in run.stdout
    assert re.search(
        r"^deadlift: would delete 1 of 2 versions of web \(a dry run\)\.$", run.stderr, re.M
    )
    assert fake.tags("web") == ["sha-c1", "sha-c2"]


def test_finds_the_images_of_a_repository_whose_name_has_capitals_as_ghcr_lowercases_them(
    github: MakeRegistry,
) -> None:
    fake = github({"web": [version(["sha-c1"], 2), version(["sha-c2"], 1)]})

    run = fake.fake.run(SCRIPT, ["--keep", "sha-c2", "web"], {"GITHUB_REPOSITORY": "Acme/Shop"})

    assert run.status == 0, run.stderr
    assert fake.tags("web") == ["sha-c2"]


@pytest.mark.parametrize(
    ("args", "env"),
    [
        pytest.param(["--keep", "sha-c1"], {}, id="no images"),
        pytest.param(["--keep", "sha-c1", "../web"], {}, id="an image name that isn't one"),
        pytest.param(["--keep", "sha-c1", "--force", "web"], {}, id="an unknown option"),
        pytest.param(["--keep", "sha-c1", "--keep-none", "web"], {}, id="both keep and keep-none"),
        pytest.param(
            ["--keep", "sha-c1", "web"],
            {"GITHUB_REPOSITORY": "shop"},
            id="a repository that isn't <owner>/<name>",
        ),
    ],
)
def test_refuses_deleting_nothing(
    github: MakeRegistry, args: list[str], env: dict[str, str]
) -> None:
    fake = github({"web": [version(["sha-c1"], 2), version(["sha-c2"], 1)]})

    run = fake.fake.run(SCRIPT, args, {"GITHUB_REPOSITORY": "acme/shop", **env})

    assert run.status == 2
    assert re.search(r"^deadlift: ", run.stderr, re.M)
    assert fake.tags("web") == ["sha-c1", "sha-c2"]


class TestAsAnAction:
    @staticmethod
    def two() -> dict[str, list[PackageVersion]]:
        return {
            "web": [version(["sha-c1"], 2), version(["sha-c2"], 1)],
            "worker": [version(["sha-c1"], 2), version(["sha-c2"], 1)],
        }

    @staticmethod
    def run_action(fake: Registry, inputs: dict[str, str]) -> ActionRun:
        return fake.fake.run_action("actions/prune-registry", inputs, {"repository": "acme/shop"})

    def test_prunes_the_images_its_given_keeping_the_listed_versions_and_writes_a_summary(
        self, github: MakeRegistry
    ) -> None:
        fake = github(self.two())

        run = self.run_action(fake, {"images": "web\nworker\n", "keep": "sha-c2\n"})

        assert run.status == 0, run.stderr
        assert fake.tags("web") == ["sha-c2"]
        assert fake.tags("worker") == ["sha-c2"]
        assert re.search(r"^deadlift: deleted 2 of 4 versions of web, worker\.$", run.summary, re.M)

    def test_refuses_an_empty_keep_list_failing_the_step(self, github: MakeRegistry) -> None:
        fake = github(self.two())

        run = self.run_action(fake, {"images": "web worker", "keep": ""})

        assert run.status == 2
        assert re.search(r"^deadlift: .*keep-none", run.summary, re.M)
        assert fake.tags("web") == ["sha-c1", "sha-c2"]

    def test_deletes_every_version_with_keep_none(self, github: MakeRegistry) -> None:
        fake = github(self.two())

        run = self.run_action(fake, {"images": "web", "keep-none": "true"})

        assert run.status == 0, run.stderr
        assert fake.tags("web") == []
        assert len(fake.tags("worker")) == 2

    def test_deletes_nothing_on_a_dry_run(self, github: MakeRegistry) -> None:
        fake = github(self.two())

        run = self.run_action(fake, {"images": "web", "keep": "sha-c2", "dry-run": "true"})

        assert run.status == 0, run.stderr
        assert "Would delete web:sha-c1" in run.summary
        assert fake.tags("web") == ["sha-c1", "sha-c2"]


def test_deletes_every_version_when_told_to_keep_none(github: MakeRegistry) -> None:
    fake = github({"web": [version(["sha-c1"], 2), version(["sha-c2"], 1)]})

    run = fake.prune("--keep-none", "web")

    assert run.status == 0, run.stderr
    assert fake.tags("web") == []
