"""The starter workflows, as `deadlift init` writes them into an App's repository, run for
GitHub's events on a fake runner (support/workflow.py) against a fake GitHub. deadlift's own
actions are stood in for: their tests (deliver-action, build-action, superseded, prune-registry)
check what they do with their inputs, and these check what each workflow asks of them, and what
it does with what they answer: the pull request's comment and the commit's check."""

from collections.abc import Callable, Sequence
from typing import Literal, NotRequired, Protocol, TypedDict, cast

import pytest

from tests.support.app_repository import AppRepository
from tests.support.expressions import Value
from tests.support.fake_github import (
    CheckRun,
    FakeGitHub,
    GitHubState,
    IssueComment,
    PullRequest,
    Repository,
)
from tests.support.matchers import Containing
from tests.support.workflow import (
    Answer,
    UsesStub,
    WorkflowEvent,
    WorkflowRun,
    run_workflow,
)

REPO = "acme/shop"


def sha(c: str) -> str:
    return c * 40


def pull(
    number: int,
    head: str,
    *,
    state: Literal["open", "closed"] = "open",
    labels: Sequence[str] = (),
    head_repo: str = REPO,
) -> PullRequest:
    return {
        "number": number,
        "state": state,
        "head": {"sha": head, "repo": {"full_name": head_repo}},
        "labels": [{"name": label} for label in labels],
    }


class DeliverAnswer(TypedDict):
    """What a stand-in deliver answers: its outputs, and a failing step for `failed`."""

    outcome: Literal["done", "skipped", "failed"]
    reason: str
    url: NotRequired[str]
    environment: NotRequired[str]
    swept: NotRequired[str]


class RepoState(TypedDict, total=False):
    branches: dict[str, str]
    commits: dict[str, list[str]]
    pulls: list[PullRequest]
    comments: list[IssueComment]


class StarterRun:
    def __init__(self, run: WorkflowRun) -> None:
        self.run = run
        self.triggered = run.triggered
        self.jobs = run.jobs
        self.used = run.used
        self.stderr = run.stderr
        self.summary = run.summary
        self.delivered = self.asked("deliver")

    def asked(self, action: str) -> list[dict[str, str]]:
        """What the workflow asked deadlift's `action` to do, step by step: its `with:`."""
        return [u.with_ for u in self.used if u.action == f"hangindev/deadlift/actions/{action}"]


class App:
    """An App's repository after `deadlift init`, with a fake GitHub holding acme/shop and
    stand-ins for the actions its workflows use. `deliver` answers with `answer`, and the
    stand-ins record what they were asked."""

    def __init__(self, repository: AppRepository, github: FakeGitHub) -> None:
        self.repository = repository
        self.github = github
        self.answer: DeliverAnswer = {"outcome": "done", "reason": "deadlift: done"}
        self.superseded = {
            "superseded": "false",
            "head": "",
            "reason": "deadlift: not superseded",
        }

        def build(inputs: dict[str, str]) -> Answer:
            names = [line.split(" ")[0] for line in inputs["images"].split("\n") if line]
            commit = inputs.get("commit", "push")
            return Answer(
                outputs={
                    "images": "\n".join(
                        f"{name}=ghcr.io/{REPO}/{name}:sha-{commit}" for name in names
                    )
                }
            )

        def deliver(_: dict[str, str]) -> Answer:
            # Every DeliverAnswer value is a string.
            outputs = {
                "environment": "",
                "url": "",
                "swept": "",
                **cast(dict[str, str], self.answer),
            }
            return Answer(status=1 if self.answer["outcome"] == "failed" else 0, outputs=outputs)

        self.stubs: dict[str, UsesStub] = {
            "actions/checkout": lambda _: None,
            "hangindev/deadlift/actions/build": build,
            "hangindev/deadlift/actions/deliver": deliver,
            "hangindev/deadlift/actions/superseded": lambda _: Answer(outputs=self.superseded),
            "hangindev/deadlift/actions/prune-registry": lambda _: None,
        }

    def run(
        self, workflow: str, event: WorkflowEvent, context: dict[str, Value] | None = None
    ) -> StarterRun:
        return StarterRun(
            run_workflow(
                self.repository.workflow(workflow),
                event,
                github={"repository": REPO, "token": "fake-job-token", **(context or {})},
                vars={
                    "TS_OAUTH_CLIENT_ID": "client-123",
                    "TS_AUDIENCE": "api.tailscale.com/client-123",
                },
                uses=self.stubs,
                env=self.github.env,
                cwd=self.repository.dir,
            )
        )

    def deliver_answers(self, answer: DeliverAnswer) -> None:
        self.answer = answer

    def superseded_answers(self, superseded: dict[str, str]) -> None:
        self.superseded = superseded

    def comments(self, pr: int) -> list[IssueComment]:
        repo = self.github.state().get("repos", {})[REPO]
        return [c for c in repo.get("comments", []) if c["issue"] == pr]

    def checks(self) -> list[CheckRun]:
        return self.github.state().get("repos", {})[REPO].get("checkRuns", [])

    def pull_request(
        self, action: str, pr: PullRequest, extra: dict[str, Value] | None = None
    ) -> StarterRun:
        """A pull_request event for this pull request, as GitHub sends it."""
        return self.run(
            "deadlift-feature",
            WorkflowEvent(
                "pull_request",
                payload={
                    "action": action,
                    "number": pr["number"],
                    "pull_request": cast(Value, pr),
                    **(extra or {}),
                },
            ),
        )

    def set_pulls(self, pulls: list[PullRequest]) -> None:
        def change(state: GitHubState) -> None:
            state.get("repos", {})[REPO]["pulls"] = pulls

        self.github.update(change)

    def fail_listing_pulls(self) -> None:
        def change(state: GitHubState) -> None:
            state["failures"] = [
                {"method": "GET", "pathPrefix": f"repos/{REPO}/pulls", "status": 502}
            ]

        self.github.update(change)


class MakeApp(Protocol):
    def __call__(self, state: RepoState | None = None, init_args: Sequence[str] = ()) -> App: ...


@pytest.fixture
def app(
    make_app_repository: Callable[[], AppRepository],
    make_github: Callable[[GitHubState], FakeGitHub],
) -> MakeApp:
    def make(state: RepoState | None = None, init_args: Sequence[str] = ()) -> App:
        repository = make_app_repository()
        init = repository.init(["shop", "--server", "deadlift-1", *init_args])
        if init.status != 0:
            raise RuntimeError(init.stderr)
        state = state or {}
        repo: Repository = {
            "ownerType": "Organization",
            "branches": state.get("branches", {}),
            "commits": state.get("commits", {}),
        }
        if "pulls" in state:
            repo["pulls"] = state["pulls"]
        if "comments" in state:
            repo["comments"] = state["comments"]
        github = make_github({"repos": {REPO: repo}})
        return App(repository, github)

    return make


def up(n: int) -> DeliverAnswer:
    return {
        "outcome": "done",
        "reason": f"deadlift: deployed feature-{n}, which is up at https://shop.tailtest.ts.net:10001/",
        "url": "https://shop.tailtest.ts.net:10001/",
        "environment": f"feature-{n}",
    }


class TestDeadliftFeature:
    def test_deploys_a_pull_requests_feature_environment_keyed_by_its_number_keeping_every_open_ones(  # noqa: E501
        self, app: MakeApp
    ) -> None:
        pr = pull(42, sha("a"))
        fork = pull(60, sha("f"), head_repo="someone/shop")
        fake = app({"pulls": [pull(57, sha("b")), pr, fork, pull(13, sha("c"), state="closed")]})
        fake.deliver_answers(up(42))

        run = fake.pull_request("opened", pr)

        assert run.jobs["deliver"].result == "success", run.stderr
        assert run.asked("build") == [Containing({"commit": sha("a"), "images": "web\n"})]
        assert run.delivered == [
            Containing(
                {
                    "app": "shop",
                    "server": "deadlift-1",
                    "operation": "deploy",
                    "environment": "feature-42",
                    "images": f"web=ghcr.io/{REPO}/web:sha-{sha('a')}",
                    "keep": "57,42",
                    "oauth-client-id": "client-123",
                    "audience": "api.tailscale.com/client-123",
                }
            )
        ]


class TestDeadliftFeaturesCommentAndCheck:
    def test_comments_the_environments_address_and_sets_a_successful_check_on_the_commit(
        self, app: MakeApp
    ) -> None:
        pr = pull(42, sha("a"))
        fake = app({"pulls": [pr]})
        fake.deliver_answers(up(42))

        fake.pull_request("opened", pr)

        comments = fake.comments(42)
        assert len(comments) == 1
        assert "https://shop.tailtest.ts.net:10001/" in comments[0]["body"]
        assert "`aaaaaaa`" in comments[0]["body"]
        assert fake.checks() == [
            Containing(
                {
                    "name": "Feature Environment",
                    "head_sha": sha("a"),
                    "status": "completed",
                    "conclusion": "success",
                }
            )
        ]

    def test_keeps_one_comment_edited_in_place_as_the_pull_request_moves_on(
        self, app: MakeApp
    ) -> None:
        fake = app({"pulls": [pull(42, sha("a"))]})
        fake.deliver_answers(up(42))
        fake.pull_request("opened", pull(42, sha("a")))
        fake.set_pulls([pull(42, sha("b"))])

        fake.pull_request("synchronize", pull(42, sha("b")))

        comments = fake.comments(42)
        assert len(comments) == 1
        assert "`bbbbbbb`" in comments[0]["body"]
        assert "`aaaaaaa`" not in comments[0]["body"]

    def test_leaves_other_comments_alone(self, app: MakeApp) -> None:
        fake = app(
            {
                "pulls": [pull(42, sha("a"))],
                "comments": [{"id": 1, "issue": 42, "body": "Looks good"}],
            }
        )
        fake.deliver_answers(up(42))

        fake.pull_request("opened", pull(42, sha("a")))

        assert len(fake.comments(42)) == 2
        assert fake.comments(42)[0]["body"] == "Looks good"

    def test_says_why_when_the_deploy_was_skipped_with_a_neutral_check(self, app: MakeApp) -> None:
        pr = pull(42, sha("a"))
        fake = app({"pulls": [pr]})
        reason = "deadlift: no Slot is free for feature-42: feature-7 and feature-9 hold them."
        fake.deliver_answers({"outcome": "skipped", "reason": reason, "environment": "feature-42"})

        run = fake.pull_request("opened", pr)

        assert run.jobs["deliver"].result == "success", run.stderr
        assert reason in fake.comments(42)[0]["body"]
        assert fake.checks() == [
            Containing(
                {
                    "head_sha": sha("a"),
                    "conclusion": "neutral",
                    "output": Containing({"summary": reason}),
                }
            )
        ]

    def test_says_why_when_the_deploy_failed_and_fails(self, app: MakeApp) -> None:
        pr = pull(42, sha("a"))
        fake = app({"pulls": [pr]})
        reason = (
            "deadlift: feature-42's routed service web isn't healthy, so feature-42 is stopped."
        )
        fake.deliver_answers({"outcome": "failed", "reason": reason, "environment": "feature-42"})

        run = fake.pull_request("opened", pr)

        assert run.jobs["deliver"].result == "failure"
        assert reason in fake.comments(42)[0]["body"]
        assert "https://github.com/acme/shop/actions/runs/" in fake.comments(42)[0]["body"]


class TestDeadliftFeaturesConditions:
    def test_builds_and_delivers_nothing_for_a_pull_request_with_the_skip_deploy_label(
        self, app: MakeApp
    ) -> None:
        pr = pull(42, sha("a"), labels=["skip-deploy"])
        fake = app({"pulls": [pr]})

        run = fake.pull_request("synchronize", pr)

        assert run.used == []
        assert fake.comments(42) == []

    def test_delivers_nothing_when_the_label_was_added_while_the_run_waited(
        self, app: MakeApp
    ) -> None:
        fake = app({"pulls": [pull(42, sha("a"), labels=["skip-deploy"])]})

        run = fake.pull_request("synchronize", pull(42, sha("a")))

        assert run.jobs["deliver"].result == "success", run.stderr
        assert run.delivered == []

    def test_deploys_once_the_skip_deploy_label_is_removed_and_not_for_other_labels(
        self, app: MakeApp
    ) -> None:
        pr = pull(42, sha("a"))
        fake = app({"pulls": [pr]})

        assert fake.pull_request("unlabeled", pr, {"label": {"name": "skip-deploy"}}).delivered == [
            Containing({"operation": "deploy", "environment": "feature-42"})
        ]
        assert fake.pull_request("unlabeled", pr, {"label": {"name": "docs"}}).used == []

    def test_delivers_nothing_when_the_pull_request_closed_or_moved_on_while_the_run_waited(
        self, app: MakeApp
    ) -> None:
        fake = app({"pulls": [pull(42, sha("a"), state="closed"), pull(57, sha("c"))]})

        assert fake.pull_request("synchronize", pull(42, sha("a"))).delivered == []
        assert fake.pull_request("synchronize", pull(57, sha("b"))).delivered == []

    def test_delivers_nothing_and_fails_when_it_cant_list_the_open_pull_requests(
        self, app: MakeApp
    ) -> None:
        pr = pull(42, sha("a"))
        fake = app({"pulls": [pr]})
        fake.fail_listing_pulls()

        run = fake.pull_request("opened", pr)

        assert run.jobs["deliver"].result == "failure"
        assert run.delivered == []

    def test_does_nothing_for_a_pull_request_from_a_fork(self, app: MakeApp) -> None:
        pr = pull(42, sha("a"), head_repo="someone/shop")
        fake = app({"pulls": [pr]})

        assert fake.pull_request("opened", pr).used == []
        assert fake.pull_request("closed", {**pr, "state": "closed"}).used == []


class TestDeadliftFeatureOnClose:
    def test_destroys_the_feature_environment_and_says_so_in_the_comment(
        self, app: MakeApp
    ) -> None:
        fake = app({"pulls": [pull(42, sha("a"))]})
        fake.deliver_answers(up(42))
        fake.pull_request("opened", pull(42, sha("a")))
        closed = pull(42, sha("a"), state="closed")
        fake.set_pulls([closed])
        fake.deliver_answers(
            {
                "outcome": "done",
                "reason": "deadlift: destroyed feature-42",
                "environment": "feature-42",
            }
        )

        run = fake.pull_request("closed", closed)

        assert run.asked("build") == []
        assert run.delivered == [
            Containing(
                {
                    "app": "shop",
                    "server": "deadlift-1",
                    "operation": "destroy",
                    "environment": "feature-42",
                }
            )
        ]
        assert len(fake.comments(42)) == 1
        assert "destroyed" in fake.comments(42)[0]["body"]

    def test_destroys_it_even_with_the_skip_deploy_label_and_starts_no_comment(
        self, app: MakeApp
    ) -> None:
        closed = pull(42, sha("a"), state="closed", labels=["skip-deploy"])
        fake = app({"pulls": [closed]})

        run = fake.pull_request("closed", closed)

        assert run.delivered == [Containing({"operation": "destroy", "environment": "feature-42"})]
        assert fake.comments(42) == []

    def test_says_when_the_destroy_failed_and_fails(self, app: MakeApp) -> None:
        fake = app({"pulls": [pull(42, sha("a"))]})
        fake.deliver_answers(up(42))
        fake.pull_request("opened", pull(42, sha("a")))
        reason = "deadlift: couldn't reach deadlift-1 over SSH, so nothing was changed."
        fake.deliver_answers({"outcome": "failed", "reason": reason})

        run = fake.pull_request("closed", pull(42, sha("a"), state="closed"))

        assert run.jobs["deliver"].result == "failure"
        assert reason in fake.comments(42)[0]["body"]


def push(fake: App, branch: str, commit: str | None = None) -> StarterRun:
    return fake.run(
        "deadlift-dev",
        WorkflowEvent("push", payload={"ref": f"refs/heads/{branch}"}),
        {"ref": f"refs/heads/{branch}", "sha": commit or sha("d")},
    )


class TestDeadliftDev:
    def test_builds_a_push_to_main_and_deploys_it_to_dev_unless_main_has_moved_past_it(
        self, app: MakeApp
    ) -> None:
        fake = app()

        run = push(fake, "main")

        assert run.jobs["deploy"].result == "success", run.stderr
        assert run.asked("superseded") == [Containing({"branch": "main"})]
        assert run.asked("build") == [Containing({"images": "web\n"})]
        assert run.delivered == [
            Containing(
                {
                    "app": "shop",
                    "server": "deadlift-1",
                    "operation": "deploy",
                    "environment": "dev",
                    "images": f"web=ghcr.io/{REPO}/web:sha-push",
                }
            )
        ]

    def test_deploys_nothing_when_main_has_moved_past_the_commit(self, app: MakeApp) -> None:
        fake = app()
        fake.superseded_answers(
            {
                "superseded": "true",
                "head": sha("e"),
                "reason": f"deadlift: main has moved past {sha('d')} to {sha('e')}",
            }
        )

        run = push(fake, "main")

        assert run.jobs["deploy"].result == "success", run.stderr
        assert run.delivered == []
        assert "main has moved past" in run.summary

    def test_runs_for_pushes_to_the_tracked_branch_only_which_init_can_change(
        self, app: MakeApp
    ) -> None:
        assert push(app(), "feature/x").triggered is False
        release = app({}, ["--branch", "release/2.x"])
        assert push(release, "main").triggered is False
        run = push(release, "release/2.x")
        assert run.asked("superseded") == [Containing({"branch": "release/2.x"})]
        assert len(run.delivered) == 1

    def test_builds_and_deploys_every_image_init_was_given(self, app: MakeApp) -> None:
        fake = app({}, ["--image", "web", "--image", "worker"])

        run = push(fake, "main")

        assert run.asked("build") == [Containing({"images": "web\nworker\n"})]
        assert run.delivered[0]["images"] == (
            f"web=ghcr.io/{REPO}/web:sha-push\nworker=ghcr.io/{REPO}/worker:sha-push"
        )


def sweep(fake: App, event: str = "schedule") -> StarterRun:
    return fake.run("deadlift-sweep", WorkflowEvent(event))


# main's history: c1 <- c2 <- ... <- c12.
HISTORY = [f"{i + 1:02d}".ljust(40, "c") for i in range(12)]
MAIN: RepoState = {
    "branches": {"main": HISTORY[11]},
    "commits": {c: [HISTORY[i - 1]] if i else [] for i, c in enumerate(HISTORY)},
}


class TestDeadliftSweep:
    def test_sweeps_every_feature_environment_but_the_open_pull_requests_then_prunes_the_registry(
        self, app: MakeApp
    ) -> None:
        fake = app(
            {
                **MAIN,
                "pulls": [
                    pull(42, sha("a")),
                    pull(57, sha("b")),
                    pull(60, sha("f"), head_repo="someone/shop"),
                    pull(13, sha("c"), state="closed"),
                ],
            }
        )
        fake.deliver_answers(
            {"outcome": "done", "reason": "deadlift: swept feature-13", "swept": "13"}
        )

        run = sweep(fake)

        assert run.jobs["prune-registry"].result == "success", run.stderr
        assert run.delivered == [
            Containing(
                {
                    "app": "shop",
                    "server": "deadlift-1",
                    "operation": "sweep",
                    "keep": "42,57",
                    "keep-none": "false",
                }
            )
        ]
        pruned = run.asked("prune-registry")
        assert len(pruned) == 1
        assert pruned[0]["images"] == "web"
        # The open pull requests' heads, and main's last 10 commits.
        assert sorted(pruned[0]["keep"].split(",")) == sorted(
            [f"sha-{sha('a')}", f"sha-{sha('b')}", *(f"sha-{c}" for c in HISTORY[2:])]
        )

    def test_keeps_none_when_no_pull_request_is_open(self, app: MakeApp) -> None:
        fake = app({**MAIN, "pulls": [pull(13, sha("c"), state="closed")]})

        run = sweep(fake, "workflow_dispatch")

        assert run.delivered == [
            Containing({"operation": "sweep", "keep": "", "keep-none": "true"})
        ]

    def test_sweeps_nothing_and_prunes_nothing_when_it_cant_list_the_open_pull_requests(
        self, app: MakeApp
    ) -> None:
        fake = app({**MAIN, "pulls": [pull(42, sha("a"))]})
        fake.fail_listing_pulls()

        run = sweep(fake)

        assert run.jobs["sweep"].result == "failure"
        assert [u for u in run.used if u.action.startswith("hangindev/")] == []

    def test_prunes_nothing_when_the_sweep_failed(self, app: MakeApp) -> None:
        fake = app({**MAIN, "pulls": [pull(42, sha("a"))]})
        fake.deliver_answers(
            {"outcome": "failed", "reason": "deadlift: couldn't reach deadlift-1 over SSH"}
        )

        run = sweep(fake)

        assert run.asked("prune-registry") == []

    def test_prunes_every_image_init_was_given(self, app: MakeApp) -> None:
        fake = app({**MAIN, "pulls": [pull(42, sha("a"))]}, ["--image", "web", "--image", "worker"])

        assert sweep(fake).asked("prune-registry")[0]["images"] == "web worker"

    def test_keeps_the_tracked_branchs_commits_when_init_was_given_another_branch(
        self, app: MakeApp
    ) -> None:
        fake = app(
            {
                "branches": {"release/2.x": HISTORY[11]},
                "commits": MAIN.get("commits", {}),
                "pulls": [],
            },
            ["--branch", "release/2.x"],
        )

        run = sweep(fake)

        assert f"sha-{HISTORY[11]}" in run.asked("prune-registry")[0]["keep"]


def rollback(fake: App, inputs: dict[str, Value] | None = None) -> StarterRun:
    return fake.run("deadlift-rollback", WorkflowEvent("workflow_dispatch", inputs=inputs))


class TestDeadliftRollback:
    def test_rolls_dev_back_by_hand_keeping_todays_data_unless_asked_otherwise(
        self, app: MakeApp
    ) -> None:
        fake = app()

        assert rollback(fake).delivered == [
            Containing(
                {
                    "app": "shop",
                    "server": "deadlift-1",
                    "operation": "rollback",
                    "environment": "dev",
                    "discard-newer-data": "false",
                }
            )
        ]
        assert rollback(fake, {"discard-newer-data": True}).delivered == [
            Containing(
                {"operation": "rollback", "environment": "dev", "discard-newer-data": "true"}
            )
        ]

    def test_runs_only_by_hand(self, app: MakeApp) -> None:
        assert app().run("deadlift-rollback", WorkflowEvent("push")).triggered is False


class TestTheStarterWorkflows:
    def test_use_deadlifts_actions_at_v1(self, app: MakeApp) -> None:
        fake = app(
            {
                "pulls": [pull(42, sha("a"))],
                "branches": {"main": sha("d")},
                "commits": {sha("d"): []},
            }
        )
        runs = [
            fake.pull_request("opened", pull(42, sha("a"))),
            fake.run(
                "deadlift-dev",
                WorkflowEvent("push"),
                {"ref": "refs/heads/main", "sha": sha("d")},
            ),
            fake.run("deadlift-sweep", WorkflowEvent("schedule")),
            fake.run("deadlift-rollback", WorkflowEvent("workflow_dispatch")),
        ]

        deadlift_actions = [
            u for r in runs for u in r.used if u.action.startswith("hangindev/deadlift/")
        ]
        assert {u.action for u in deadlift_actions} == {
            f"hangindev/deadlift/actions/{a}"
            for a in ["build", "deliver", "superseded", "prune-registry"]
        }
        assert {u.ref for u in deadlift_actions} == {"v1"}
