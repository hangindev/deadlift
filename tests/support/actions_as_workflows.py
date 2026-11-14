"""Writes each of deadlift's actions (actions/*/action.yml) out as a workflow actionlint can check.

actionlint checks workflows, not composite actions. Each becomes a reusable workflow whose
inputs are the action's, with one job running the action's steps and giving the action's
outputs. actionlint then checks the steps' expressions, the inputs of the actions they use, and
shellcheck over every `run:` script.

    python -m tests.support.actions_as_workflows <directory>
"""

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from tests.support.composite_action import REPO_ROOT
from tests.support.github_yaml import load


def write_workflows(out: Path) -> None:
    for action_dir in sorted((REPO_ROOT / "actions").iterdir()):
        action: dict[str, Any] = load((action_dir / "action.yml").read_text())
        output_specs: dict[str, dict[str, Any]] = action.get("outputs") or {}
        input_specs: dict[str, dict[str, Any] | None] = action.get("inputs") or {}
        outputs = {name: spec["value"] for name, spec in output_specs.items()}
        job: dict[str, Any] = {"runs-on": "ubuntu-24.04"}
        # actionlint refuses an empty section.
        if outputs:
            job["outputs"] = outputs
        job["steps"] = action["runs"]["steps"]
        workflow = {
            "name": f"{action['name']} (action.yml as a workflow, for actionlint)",
            "on": {
                "workflow_call": {
                    "inputs": {
                        name: {"type": "string", "required": bool((spec or {}).get("required"))}
                        for name, spec in input_specs.items()
                    }
                }
            },
            "jobs": {"action": job},
        }
        (out / f"action-{action_dir.name}.yml").write_text(
            yaml.safe_dump(workflow, sort_keys=False, allow_unicode=True)
        )


def main(argv: Sequence[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: python -m tests.support.actions_as_workflows <directory>\n")
        return 2
    write_workflows(Path(argv[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
