"""Says whether the tracked branch has moved past a commit, so a Dev workflow can skip a deploy that
a newer commit's own run deploys, instead of putting older code on the Dev Environment.

Usage: superseded.py <branch> <commit>
Writes ``superseded=true|false``, ``head=<the branch's head>`` and ``reason=`` to $GITHUB_OUTPUT
(or stdout). A commit the branch no longer holds, after a force-push, counts as superseded too.
Needs GH_TOKEN with contents: read, and GITHUB_REPOSITORY. Asks GitHub, so it needs no clone.
Exits 0 with an answer, 1 when GitHub couldn't say (no answer), 2 on bad usage.
"""

import os
import re
import sys
from collections.abc import Sequence

from deadlift.actions import job
from deadlift.core.outcome import REASON_PREFIX, Exit, Stop, fail, reason, refuse, run_main
from deadlift.core.process import Env, Stream, run

_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_COMMIT = re.compile(r"[0-9a-f]{7,40}")
# Plain branch names only, so the name can go into the API's paths as it is.
_BRANCH = re.compile(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*")


def check(branch: str, commit: str, repository: str) -> None:
    """Refuses (Stop) a repository, commit or branch this can't ask GitHub about."""
    if not _REPOSITORY.fullmatch(repository):
        refuse(f"GITHUB_REPOSITORY must be <owner>/<name>, not '{repository}'.")
    if not _COMMIT.fullmatch(commit):
        refuse(f"the commit must be a SHA, not '{commit}'.")
    if not _BRANCH.fullmatch(branch) or ".." in branch or branch.startswith("."):
        refuse(f"the branch must be a plain branch name such as main, not '{branch}'.")


def verdict(branch: str, commit: str, head: str, status: str) -> tuple[bool, str]:
    """Whether ``commit`` is superseded, and why, from how GitHub compares it with ``head``.
    Fails (Stop) on a comparison this doesn't know."""
    match status:
        case "identical":
            return False, f"{commit} is {branch}'s head, so it's the newest."
        case "ahead":
            return True, f"{branch} has moved past {commit} to {head}, whose own run deploys it."
        case "behind" | "diverged":
            return True, f"{commit} isn't in {branch}'s history any more ({status})."
        case _:
            raise Stop(
                Exit.FAILED,
                f"GitHub compared {commit} with {branch}'s head {head} as '{status}',"
                " which this doesn't know.",
            )


def _ask(path: str, jq: str, env: Env) -> str | None:
    """``gh api <path> --jq <jq>``'s answer, or None when it failed."""
    answer = run(["gh", "api", path, "--jq", jq], env=env, stdout=Stream.CAPTURE)
    return answer.stdout.rstrip("\n") if answer.ok else None


def _decide(superseded: bool, why: str, head: str) -> Exit:
    reason(why)
    lines = [
        f"superseded={'true' if superseded else 'false'}",
        f"head={head}",
        f"reason={REASON_PREFIX}{why}",
    ]
    if not job.append("GITHUB_OUTPUT", lines):
        sys.stdout.writelines(f"{line}\n" for line in lines)
        sys.stdout.flush()
    return Exit.DONE


def _superseded(argv: Sequence[str]) -> Exit:
    if len(argv) != 2:
        refuse("usage: superseded.py <branch> <commit>")
    branch, commit = argv
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    check(branch, commit, repository)
    env = job.job_env()

    head = _ask(f"repos/{repository}/git/ref/heads/{branch}", ".object.sha", env)
    if head is None:
        fail(f"couldn't find {branch}'s head in {repository}, so this says nothing either way.")
    if commit == head:
        return _decide(*verdict(branch, commit, head, "identical"), head)

    status = _ask(f"repos/{repository}/compare/{commit}...{head}", ".status", env)
    if status is None:
        fail(
            f"couldn't compare {commit} with {branch}'s head {head},"
            " so this says nothing either way."
        )
    return _decide(*verdict(branch, commit, head, status), head)


def main(argv: Sequence[str]) -> int:
    return run_main(lambda: _superseded(argv))
