"""Runs a workflow as GitHub would for one event, far enough to test the starter workflows.

Whether the event triggers it, its jobs in `needs` order with their `if:`, `env`, `outputs` and
the `needs` context, and each job's steps: `run:` steps in bash, with GITHUB_OUTPUT and
GITHUB_STEP_SUMMARY, and `uses:` steps through the test's stand-ins, which get the step's `with:`
and answer with an exit code and outputs. Expressions are GitHub's (expressions.py).
Concurrency, permissions and runners aren't modelled; actionlint checks those are well formed.
"""

import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from tests.support.composite_action import parse_output_file
from tests.support.expressions import Scope, Value, condition, interpolate, to_text
from tests.support.github_yaml import load


@dataclass(frozen=True)
class Answer:
    """What a stand-in for a `uses:` step answers: its exit code and outputs."""

    status: int = 0
    outputs: dict[str, str] = field(default_factory=dict[str, str])


type UsesStub = Callable[[dict[str, str]], Answer | None]
"""A test's stand-in for an action a step `uses:`. It gets the step's `with:`."""


@dataclass(frozen=True)
class JobRun:
    result: Literal["success", "failure", "skipped"]
    outputs: dict[str, str]


@dataclass(frozen=True)
class Used:
    """A `uses:` step that ran: the action without its `@ref`, the ref, and its `with:`."""

    action: str
    ref: str
    with_: dict[str, str]


@dataclass
class WorkflowRun:
    triggered: bool = False
    """Whether the event triggers the workflow at all. When it doesn't, no job ran."""
    jobs: dict[str, JobRun] = field(default_factory=dict[str, JobRun])
    used: list[Used] = field(default_factory=list[Used])
    """Each `uses:` step that ran, in order."""
    stdout: str = ""
    stderr: str = ""
    summary: str = ""
    """Every job's summary, run by run."""


@dataclass(frozen=True)
class WorkflowEvent:
    name: str
    """The event's name, such as `pull_request`, `push`, `schedule` or `workflow_dispatch`."""
    payload: dict[str, Value] | None = None
    """The event's payload, `github.event`."""
    inputs: dict[str, Value] | None = None
    """For `workflow_dispatch`: the inputs given, before defaults."""


def run_workflow(
    file: Path,
    event: WorkflowEvent,
    *,
    github: Mapping[str, Value] | None = None,
    vars: Mapping[str, str] | None = None,
    uses: Mapping[str, UsesStub] | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> WorkflowRun:
    """Runs the workflow in `file` for `event`, with the rest of the `github` context
    (repository, sha, ref, token, …), `vars`, stand-ins for the actions the steps use (by name
    without the `@ref`), the runner's own environment (PATH, HOME, …) and the job's working
    directory."""
    github = dict(github or {})
    uses = uses or {}
    workflow: dict[str, Any] = load(file.read_text())
    run = WorkflowRun()
    if not _triggers(workflow, event, github):
        return run
    run.triggered = True

    ref = to_text(github.get("ref", ""))
    event_context: dict[str, Value] = dict(event.payload or {})
    if event.inputs is not None:
        event_context["inputs"] = dict(event.inputs)
    github_context: dict[str, Value] = {
        "event_name": event.name,
        "event": event_context,
        "ref_name": re.sub(r"^refs/(heads|tags)/", "", ref),
        "server_url": "https://github.com",
        "run_id": "1234",
        **github,
    }
    inputs = _dispatch_inputs(workflow, event)
    base: dict[str, Value] = {
        "github": github_context,
        "vars": dict(vars or {}),
        "inputs": inputs,
        "secrets": {},
    }
    workflow_env = _evaluate_all(
        workflow.get("env"), Scope(base, success=True, failure=False, cancelled=False)
    )
    shell = _mapping(_mapping(workflow.get("defaults")).get("run")).get("shell")

    work = Path(tempfile.mkdtemp(prefix="deadlift-test-workflow-"))
    try:
        jobs: dict[str, dict[str, Any]] = workflow["jobs"]
        for name in _job_order(jobs):
            job = jobs[name]
            needs = _needs(job)
            needs_context: dict[str, Value] = {
                n: {"result": run.jobs[n].result, "outputs": dict(run.jobs[n].outputs)}
                for n in needs
            }
            job_scope = Scope(
                {**base, "env": dict(workflow_env), "needs": needs_context},
                success=all(run.jobs[n].result == "success" for n in needs),
                failure=any(run.jobs[n].result == "failure" for n in needs),
                cancelled=False,
            )
            if not condition(to_text(job.get("if", "success()")), job_scope):
                run.jobs[name] = JobRun("skipped", {})
                continue
            job_env = {**workflow_env, **_evaluate_all(job.get("env"), job_scope)}
            steps: dict[str, Value] = {}
            failed = False
            summary_file = work / f"summary-{name}"
            summary_file.write_text("")

            for index, step in enumerate(job["steps"]):
                scope = _step_scope(base, job_env, needs_context, steps, failed=failed)
                if not condition(to_text(step.get("if", "success()")), scope):
                    if "id" in step:
                        steps[step["id"]] = {
                            "outcome": "skipped",
                            "conclusion": "skipped",
                            "outputs": {},
                        }
                    continue
                outputs: dict[str, str]
                if "uses" in step:
                    action, _, action_ref = str(step["uses"]).partition("@")
                    stub = uses.get(action)
                    if stub is None:
                        raise ValueError(
                            f"the test gave no stand-in for {action} ({name} step {index})"
                        )
                    with_inputs = _evaluate_all(step.get("with"), scope)
                    run.used.append(Used(action, action_ref, with_inputs))
                    answer = stub(with_inputs) or Answer()
                    step_status = answer.status
                    outputs = dict(answer.outputs)
                else:
                    if "run" not in step or step.get("shell", shell) != "bash":
                        raise ValueError(
                            f"the test runner only runs bash steps ({name} step {index})"
                        )
                    output_file = work / f"output-{name}-{index}"
                    output_file.write_text("")
                    step_env = _evaluate_all(step.get("env"), scope)
                    # As the runner does for `shell: bash`.
                    result = subprocess.run(
                        [
                            "bash",
                            "--noprofile",
                            "--norc",
                            "-eo",
                            "pipefail",
                            "-c",
                            interpolate(step["run"], scope),
                        ],
                        cwd=cwd,
                        env={
                            **(env or {}),
                            **job_env,
                            **step_env,
                            "CI": "true",
                            "GITHUB_ACTIONS": "true",
                            "GITHUB_OUTPUT": str(output_file),
                            "GITHUB_STEP_SUMMARY": str(summary_file),
                        },
                        capture_output=True,
                        encoding="utf-8",
                        errors="replace",
                        check=False,
                    )
                    run.stdout += result.stdout
                    run.stderr += result.stderr
                    step_status = result.returncode if result.returncode >= 0 else 1
                    outputs = parse_output_file(output_file.read_text())
                outcome = "success" if step_status == 0 else "failure"
                if "id" in step:
                    steps[step["id"]] = {
                        "outcome": outcome,
                        "conclusion": outcome,
                        "outputs": dict(outputs),
                    }
                if step_status != 0:
                    failed = True
            run.summary += summary_file.read_text()
            run.jobs[name] = JobRun(
                "failure" if failed else "success",
                _evaluate_all(
                    job.get("outputs"),
                    _step_scope(base, job_env, needs_context, steps, failed=failed),
                ),
            )
        return run
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _step_scope(
    base: dict[str, Value],
    job_env: dict[str, str],
    needs: dict[str, Value],
    steps: dict[str, Value],
    *,
    failed: bool,
) -> Scope:
    return Scope(
        {**base, "env": dict(job_env), "needs": needs, "steps": steps},
        success=not failed,
        failure=failed,
        cancelled=False,
    )


def _mapping(value: Any) -> dict[str, Any]:
    """A mapping from the workflow, or an empty one where the workflow has none."""
    return value or {}


def _evaluate_all(values: Mapping[str, Any] | None, scope: Scope) -> dict[str, str]:
    return {key: interpolate(to_text(v), scope) for key, v in (values or {}).items()}


def _needs(job: Mapping[str, Any]) -> list[str]:
    needs: str | list[str] = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _triggers(
    workflow: Mapping[str, Any], event: WorkflowEvent, github: Mapping[str, Value]
) -> bool:
    on: dict[str, Any] = workflow["on"]
    if event.name not in on:
        return False
    filter_: dict[str, Any] = on[event.name] or {}
    if event.name in ("pull_request", "pull_request_target"):
        types: list[str] | None = filter_.get("types")
        if types is None:
            types = ["opened", "synchronize", "reopened"]
        if to_text((event.payload or {}).get("action")) not in types:
            return False
    if event.name == "push" and filter_.get("branches") is not None:
        branch = re.sub(r"^refs/heads/", "", to_text(github.get("ref", "")))
        branches: list[str] = filter_["branches"]
        if not any(_glob(pattern).fullmatch(branch) for pattern in branches):
            return False
    return True


def _glob(pattern: str) -> re.Pattern[str]:
    parts = re.split(r"(\*\*|\*)", pattern)
    return re.compile(
        "".join(".*" if p == "**" else "[^/]*" if p == "*" else re.escape(p) for p in parts)
    )


def _dispatch_inputs(workflow: Mapping[str, Any], event: WorkflowEvent) -> dict[str, Value]:
    if event.name != "workflow_dispatch":
        return {}
    specs = _mapping(_mapping(workflow["on"].get("workflow_dispatch")).get("inputs"))
    given = event.inputs or {}
    for name in given:
        if name not in specs:
            raise ValueError(f"the workflow has no input {name}")
    inputs: dict[str, Value] = {}
    for name, spec in specs.items():
        if name in given:
            v = given[name]
        else:
            default = spec.get("default")
            v = default if default is not None else (False if spec.get("type") == "boolean" else "")
        inputs[name] = v is True or v == "true" if spec.get("type") == "boolean" else v
    return inputs


def _job_order(jobs: Mapping[str, Mapping[str, Any]]) -> list[str]:
    order: list[str] = []

    def visit(name: str, path: list[str]) -> None:
        if name in order:
            return
        if name in path:
            raise ValueError(f"jobs need each other: {' -> '.join([*path, name])}")
        if name not in jobs:
            raise ValueError(f"no job named {name}")
        for need in _needs(jobs[name]):
            visit(need, [*path, name])
        order.append(name)

    for name in jobs:
        visit(name, [])
    return order
