"""Runs one of deadlift's composite actions as a GitHub runner would, far enough to test it.

Its inputs (with defaults), its `bash` steps with GITHUB_ACTION_PATH, GITHUB_OUTPUT and
GITHUB_STEP_SUMMARY, and its outputs. Expressions may name only `inputs.*`, `github.*` and
`steps.<id>.outputs.*`. A step's `if:` may be only `success()` (the default), `always()` or
`failure()`. A step that `uses:` another action runs the test's stand-in for it, given the
step's `with:`, since the real one can't run here.
"""

import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from tests.support.expressions import to_text
from tests.support.github_yaml import load

REPO_ROOT = Path(__file__).resolve().parents[2]

type UsesStub = Callable[[dict[str, str]], int | None]
"""A test's stand-in for an action a step `uses:`. It gets the step's `with:` and returns its exit
code, 0 when it returns None."""


@dataclass(frozen=True)
class ActionRun:
    status: int | None
    """The failing step's exit code, or 0 when every step passed (None if killed by a signal)."""
    stdout: str
    stderr: str
    outputs: dict[str, str]
    summary: str


def run_action(
    action: str,
    *,
    inputs: Mapping[str, str] | None = None,
    github: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
    uses: Mapping[str, UsesStub] | None = None,
    cwd: Path | None = None,
) -> ActionRun:
    """Runs the composite action at `action` (relative to the repo), with these inputs, `github`
    context, environment, stand-ins for the actions its steps use (by name without the `@ref`),
    and working directory (the job's checkout)."""
    inputs = inputs or {}
    github = github or {}
    uses = uses or {}
    action_path = REPO_ROOT / action
    yaml: dict[str, Any] = load((action_path / "action.yml").read_text())
    if yaml["runs"]["using"] != "composite":
        raise ValueError(f"{action} isn't a composite action")

    steps: dict[str, dict[str, str]] = {}
    resolved_inputs: dict[str, str] = {}

    def context(expression: str) -> str:
        scope, *path = expression.strip().split(".")
        if scope == "github":
            return github.get(".".join(path), "")
        if scope == "inputs":
            return resolved_inputs.get(".".join(path), "")
        if scope == "steps" and len(path) > 1 and path[1] == "outputs":
            return steps.get(path[0], {}).get(path[2] if len(path) > 2 else "", "")
        raise ValueError(f"the test runner can't evaluate ${{{{ {expression} }}}}")

    def evaluate(value: str) -> str:
        return re.sub(r"\$\{\{(.*?)\}\}", lambda m: context(m[1]), value)

    specs: dict[str, dict[str, Any]] = yaml.get("inputs") or {}
    for name, spec in specs.items():
        if name in inputs:
            resolved_inputs[name] = inputs[name]
        elif spec.get("required"):
            raise ValueError(f"input {name} is required")
        else:
            resolved_inputs[name] = evaluate(to_text(spec.get("default", "")))
    for name in inputs:
        if name not in resolved_inputs:
            raise ValueError(f"{action} has no input {name}")

    work = Path(tempfile.mkdtemp(prefix="deadlift-test-action-"))
    summary_file = work / "summary"
    summary_file.write_text("")
    stdout = ""
    stderr = ""
    status: int | None = 0
    try:
        for index, step in enumerate(cast(list[dict[str, Any]], yaml["runs"]["steps"])):
            condition = re.sub(r"^\$\{\{(.*)\}\}$", r"\1", str(step.get("if", "success()")))
            condition = condition.strip()
            runs = {"success()": status == 0, "always()": True, "failure()": status != 0}.get(
                condition
            )
            if runs is None:
                raise ValueError(f"the test runner can't evaluate if: {step.get('if')}")
            if not runs:
                continue
            step_status: int | None
            if "uses" in step:
                name = re.sub(r"@.*$", "", str(step["uses"]))
                stub = uses.get(name)
                if stub is None:
                    raise ValueError(f"the test gave no stand-in for {name} (step {index})")
                with_: dict[str, Any] = step.get("with") or {}
                stub_status = stub({k: evaluate(to_text(v)) for k, v in with_.items()})
                step_status = 0 if stub_status is None else stub_status
            else:
                if "run" not in step or step.get("shell") != "bash":
                    raise ValueError(
                        f"the test runner only runs `shell: bash` steps (step {index})"
                    )
                output_file = work / f"output-{index}"
                output_file.write_text("")
                step_env_spec: dict[str, Any] = step.get("env") or {}
                step_env = {k: evaluate(to_text(v)) for k, v in step_env_spec.items()}
                # As the runner does for `shell: bash`.
                result = subprocess.run(
                    [
                        "bash",
                        "--noprofile",
                        "--norc",
                        "-eo",
                        "pipefail",
                        "-c",
                        evaluate(step["run"]),
                    ],
                    cwd=cwd,
                    env={
                        **(env or {}),
                        **step_env,
                        "GITHUB_ACTION_PATH": str(action_path),
                        "GITHUB_OUTPUT": str(output_file),
                        "GITHUB_STEP_SUMMARY": str(summary_file),
                    },
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                stdout += result.stdout
                stderr += result.stderr
                if "id" in step:
                    steps[str(step["id"])] = parse_output_file(output_file.read_text())
                step_status = result.returncode if result.returncode >= 0 else None
            # The action fails with its first failing step, whatever later `always()` steps do.
            if step_status != 0 and status == 0:
                status = step_status
        output_specs: dict[str, dict[str, Any]] = yaml.get("outputs") or {}
        outputs = {name: evaluate(str(spec["value"])) for name, spec in output_specs.items()}
        return ActionRun(status, stdout, stderr, outputs, summary_file.read_text())
    finally:
        shutil.rmtree(work, ignore_errors=True)


def parse_output_file(text: str) -> dict[str, str]:
    """Parses $GITHUB_OUTPUT: `key=value` lines, and `key<<DELIMITER` blocks for multi-line
    values."""
    outputs: dict[str, str] = {}
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        heredoc = re.fullmatch(r"([^=]+)<<(.+)", lines[i])
        if heredoc:
            try:
                end = lines.index(heredoc[2], i + 1)
            except ValueError:
                raise ValueError(f"$GITHUB_OUTPUT's {heredoc[1]} has no end") from None
            outputs[heredoc[1]] = "\n".join(lines[i + 1 : end])
            i = end
        elif "=" in lines[i]:
            key, _, value = lines[i].partition("=")
            outputs[key] = value
        i += 1
    return outputs
