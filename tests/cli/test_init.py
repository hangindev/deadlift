"""``deadlift init``, run in an App's repository as its maintainer would. What the workflows it
writes do is in the starter workflows' tests."""

import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
import yaml

from tests.support.app_repository import DEADLIFT, AppRepository

STARTERS = ["deadlift-feature", "deadlift-dev", "deadlift-sweep", "deadlift-rollback"]


def workflow_name(text: str) -> object:
    """The ``name`` a workflow's YAML gives."""
    loaded: object = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    return cast("dict[object, object]", loaded).get("name")


def test_writes_the_four_starter_workflows_into_the_apps_repository(
    app_repository: AppRepository,
) -> None:
    repo = app_repository

    run = repo.init(["shop", "--server", "deadlift-1"])

    assert run.status == 0, run.stderr
    for name in STARTERS:
        assert repo.workflow(name).exists(), name
        assert workflow_name(repo.read(name)) == name
        assert f".github/workflows/{name}.yml" in run.stdout


def test_writes_them_at_the_repositorys_root_from_anywhere_in_it(
    app_repository: AppRepository,
) -> None:
    repo = app_repository
    (repo.dir / "src").mkdir()

    run = repo.init(["shop", "--server", "deadlift-1"], repo.dir / "src")

    assert run.status == 0, run.stderr
    assert repo.workflow("deadlift-dev").exists()
    assert not (repo.dir / "src/.github").exists()


@pytest.fixture
def not_a_repo() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="deadlift-test-not-a-repo-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_refuses_outside_a_git_repository_writing_nothing(not_a_repo: Path) -> None:
    result = subprocess.run(
        [str(DEADLIFT), "init", "shop", "--server", "deadlift-1"],
        cwd=not_a_repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert re.search(r"^deadlift: .*git repository", result.stderr, re.MULTILINE)
    assert not (not_a_repo / ".github").exists()


REFUSED = [
    (["Shop", "--server", "deadlift-1"], r"isn't an App name"),
    (["shop"], r"--server"),
    (["shop", "--server", "bad host"], r"isn't a host name"),
    (["shop", "--server", "deadlift-1", "--branch", "-x"], r"isn't a branch"),
    (["shop", "--server", "deadlift-1", "--image", "Web"], r"isn't an image name"),
    # As the build action names images, so init never writes a starter build refuses.
    (["shop", "--server", "deadlift-1", "--image", "1web"], r"isn't an image name"),
]


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        pytest.param(args, reason, id=json.dumps(args, separators=(",", ":")))
        for args, reason in REFUSED
    ],
)
def test_refuses_writing_nothing(
    app_repository: AppRepository, args: list[str], reason: str
) -> None:
    repo = app_repository

    run = repo.init(args)

    assert run.status == 2
    assert re.search(r"^deadlift: ", run.stderr)
    assert re.search(reason, run.stderr)
    assert not (repo.dir / ".github").exists()


def test_leaves_a_workflow_the_app_has_changed_alone_when_run_again_and_says_so(
    app_repository: AppRepository,
) -> None:
    repo = app_repository
    repo.init(["shop", "--server", "deadlift-1"])
    changed = repo.read("deadlift-feature").replace("skip-deploy", "no-preview", 1)
    repo.workflow("deadlift-feature").write_text(changed)

    run = repo.init(["shop", "--server", "deadlift-2"])

    assert run.status == 0, run.stderr
    assert repo.read("deadlift-feature") == changed
    assert re.search(r"Kept \.github/workflows/deadlift-feature\.yml.*changed", run.stdout)
    # The ones the App hasn't changed follow the new arguments.
    assert "deadlift-2" in repo.read("deadlift-dev")
    assert "deadlift-1" not in repo.read("deadlift-dev")


def test_leaves_a_workflow_of_the_apps_own_by_a_starters_name_alone(
    app_repository: AppRepository,
) -> None:
    repo = app_repository
    (repo.dir / ".github/workflows").mkdir(parents=True)
    repo.workflow("deadlift-dev").write_text("name: ours\n")

    run = repo.init(["shop", "--server", "deadlift-1"])

    assert run.status == 0, run.stderr
    assert repo.read("deadlift-dev") == "name: ours\n"
    assert repo.workflow("deadlift-feature").exists()


def test_changes_nothing_when_run_again_with_the_same_arguments(
    app_repository: AppRepository,
) -> None:
    repo = app_repository
    repo.init(["shop", "--server", "deadlift-1"])
    before = [repo.read(name) for name in STARTERS]

    run = repo.init(["shop", "--server", "deadlift-1"])

    assert run.status == 0, run.stderr
    assert [repo.read(name) for name in STARTERS] == before
    assert not re.search(r"Kept", run.stdout)


def test_tells_the_maintainer_what_the_workflows_need_from_the_repository(
    app_repository: AppRepository,
) -> None:
    run = app_repository.init(["shop", "--server", "deadlift-1"])

    assert "TS_OAUTH_CLIENT_ID" in run.stdout
    assert "TS_AUDIENCE" in run.stdout
    assert "skip-deploy" in run.stdout
