import pytest

from deadlift.actions.superseded import check, verdict
from deadlift.core.outcome import Exit, Stop

C1 = "1" * 40
C3 = "3" * 40


@pytest.mark.parametrize("branch", ["main", "release/2.x", "a_b-c.d", "feature/x/y"])
def test_a_plain_branch_name_is_accepted(branch: str) -> None:
    check(branch, C1, "acme/shop")


@pytest.mark.parametrize(
    ("branch", "commit", "repository", "why"),
    [
        ("main", C1, "shop", "GITHUB_REPOSITORY must be <owner>/<name>, not 'shop'."),
        ("main", C1, "", "GITHUB_REPOSITORY must be <owner>/<name>, not ''."),
        ("main", "HEAD~1", "acme/shop", "the commit must be a SHA, not 'HEAD~1'."),
        ("main", "abc12", "acme/shop", "the commit must be a SHA, not 'abc12'."),
        ("main", "A" * 40, "acme/shop", "the commit must be a SHA"),
        ("", C1, "acme/shop", "the branch must be a plain branch name such as main, not ''."),
        ("main/../../x", C1, "acme/shop", "the branch must be a plain branch name"),
        ("main?x=1", C1, "acme/shop", "the branch must be a plain branch name"),
        (".hidden", C1, "acme/shop", "the branch must be a plain branch name"),
        ("a..b", C1, "acme/shop", "the branch must be a plain branch name"),
        ("main/", C1, "acme/shop", "the branch must be a plain branch name"),
        ("main\n", C1, "acme/shop", "the branch must be a plain branch name"),
    ],
)
def test_a_bad_question_is_refused(branch: str, commit: str, repository: str, why: str) -> None:
    with pytest.raises(Stop) as stopped:
        check(branch, commit, repository)
    assert stopped.value.code is Exit.REFUSED
    assert (stopped.value.why or "").startswith(why)


def test_a_commit_github_finds_identical_to_the_head_is_the_newest() -> None:
    assert verdict("main", C1, C3, "identical") == (
        False,
        f"{C1} is main's head, so it's the newest.",
    )


def test_a_branch_ahead_of_the_commit_supersedes_it() -> None:
    assert verdict("main", C1, C3, "ahead") == (
        True,
        f"main has moved past {C1} to {C3}, whose own run deploys it.",
    )


@pytest.mark.parametrize("status", ["behind", "diverged"])
def test_a_commit_the_branch_no_longer_holds_is_superseded(status: str) -> None:
    assert verdict("main", C1, C3, status) == (
        True,
        f"{C1} isn't in main's history any more ({status}).",
    )


def test_a_comparison_it_doesnt_know_says_nothing_either_way() -> None:
    with pytest.raises(Stop) as stopped:
        verdict("main", C1, C3, "sideways")
    assert stopped.value.code is Exit.FAILED
    assert stopped.value.why == (
        f"GitHub compared {C1} with main's head {C3} as 'sideways', which this doesn't know."
    )
