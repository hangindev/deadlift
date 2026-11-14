"""The superseded action, driven through its script with a fake GitHub on PATH."""

import re
from collections.abc import Callable
from typing import Protocol

import pytest

from tests.support.fake_github import Failure, FakeGitHub, GitHubState, Run
from tests.support.matchers import Containing

SCRIPT = "actions/superseded/superseded.py"
REPO = "acme/shop"

# A history on main: c1 <- c2 <- c3.
C1 = "1" * 40
C2 = "2" * 40
C3 = "3" * 40


class MakeGitHub(Protocol):
    def __call__(
        self, branches: dict[str, str], commits: dict[str, list[str]] | None = None
    ) -> FakeGitHub: ...


@pytest.fixture
def github(make_github: Callable[[GitHubState], FakeGitHub]) -> MakeGitHub:
    def make(branches: dict[str, str], commits: dict[str, list[str]] | None = None) -> FakeGitHub:
        return make_github(
            {
                "repos": {
                    REPO: {
                        "ownerType": "Organization",
                        "branches": branches,
                        "commits": {C1: [], C2: [C1], C3: [C2], **(commits or {})},
                    }
                }
            }
        )

    return make


def superseded(fake: FakeGitHub, branch: str, commit: str) -> Run:
    return fake.run(SCRIPT, [branch, commit], {"GITHUB_REPOSITORY": REPO})


def test_says_a_commit_at_the_tracked_branchs_head_isnt_superseded(github: MakeGitHub) -> None:
    run = superseded(github({"main": C3}), "main", C3)

    assert run.status == 0, run.stderr
    assert run.outputs == Containing({"superseded": "false", "head": C3})


def test_says_a_commit_the_tracked_branch_has_moved_past_is_superseded(github: MakeGitHub) -> None:
    run = superseded(github({"main": C3}), "main", C1)

    assert run.status == 0, run.stderr
    assert run.outputs == Containing({"superseded": "true", "head": C3})
    assert re.search(r"^deadlift: main has moved past 1{40} to 3{40}", run.outputs["reason"])


def test_tracks_any_branch_not_only_main(github: MakeGitHub) -> None:
    fake = github({"main": C3, "release/2.x": C2})

    assert superseded(fake, "release/2.x", C2).outputs == Containing({"superseded": "false"})
    assert superseded(fake, "release/2.x", C1).outputs == Containing({"superseded": "true"})


# A force-push replaced c3 with x3, or reset main back to c2.
X3 = "a" * 40


@pytest.mark.parametrize(
    "branches",
    [
        pytest.param({"main": X3}, id="was force-pushed past"),
        pytest.param({"main": C2}, id="was reset back behind"),
    ],
)
def test_says_a_commit_is_superseded_when_the_tracked_branch_leaves_it(
    github: MakeGitHub, branches: dict[str, str]
) -> None:
    run = superseded(github(branches, {X3: [C2]}), "main", C3)

    assert run.status == 0, run.stderr
    assert run.outputs == Containing({"superseded": "true", "head": branches["main"]})
    assert re.search(r"^deadlift: 3{40} isn't in main's history any more", run.outputs["reason"])


def test_fails_saying_nothing_either_way_when_the_tracked_branch_doesnt_exist(
    github: MakeGitHub,
) -> None:
    run = superseded(github({"main": C3}), "trunk", C3)

    assert run.status == 1
    assert run.outputs == {}
    assert re.search(r"^deadlift: couldn't find trunk's head in acme/shop", run.stderr, re.M)


def test_fails_saying_nothing_either_way_when_github_cant_compare_the_commits(
    github: MakeGitHub, make_github: Callable[[GitHubState], FakeGitHub]
) -> None:
    fake = github({"main": C3})
    state = fake.state()
    failures: list[Failure] = [{"method": "GET", "pathPrefix": f"repos/{REPO}/compare/"}]
    state["failures"] = failures
    failing = make_github(state)

    run = superseded(failing, "main", C1)

    assert run.status == 1
    assert run.outputs == {}
    assert re.search(r"^deadlift: couldn't compare 1{40} with main's head 3{40}", run.stderr, re.M)


class TestAsAnAction:
    ACTION = "actions/superseded"
    CONTEXT = {"sha": C1, "repository": REPO}  # noqa: RUF012 - never changed

    def test_checks_the_workflows_own_commit_against_main_by_default(
        self, github: MakeGitHub
    ) -> None:
        fake = github({"main": C3})

        run = fake.run_action(self.ACTION, {}, self.CONTEXT)

        assert run.status == 0, run.stderr
        assert run.outputs == Containing({"superseded": "true", "head": C3})
        assert re.search(r"^deadlift: main has moved past", run.outputs["reason"])

    def test_checks_the_commit_and_branch_its_given(self, github: MakeGitHub) -> None:
        fake = github({"main": C3, "develop": C2})

        run = fake.run_action(self.ACTION, {"branch": "develop", "commit": C2}, self.CONTEXT)

        assert run.status == 0, run.stderr
        assert run.outputs == Containing({"superseded": "false", "head": C2})

    def test_fails_the_step_when_github_cant_say(self, github: MakeGitHub) -> None:
        run = github({"main": C3}).run_action(self.ACTION, {"branch": "trunk"}, self.CONTEXT)

        assert run.status == 1
        assert run.outputs["superseded"] == ""


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["main"], id="no commit"),
        pytest.param(["main", "HEAD~1"], id="a commit that isn't a SHA"),
        pytest.param(["", C3], id="an empty branch"),
        pytest.param(["main/../../x", C3], id="a branch that isn't a branch name"),
        pytest.param(["main?x=1", C3], id="a branch with a query in it"),
    ],
)
def test_refuses(github: MakeGitHub, args: list[str]) -> None:
    fake = github({"main": C3})

    run = fake.run(SCRIPT, args, {"GITHUB_REPOSITORY": REPO})

    assert run.status == 2
    assert run.outputs == {}
    assert re.search(r"^deadlift: ", run.stderr, re.M)
