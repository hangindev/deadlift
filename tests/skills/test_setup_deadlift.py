"""The setup-deadlift skill walks an agent through deadlift's own commands and policy entries, so
what it tells the agent to run must be what deadlift has, and the policy tests it adds must name
the tag and user `deadlift app add` gives the App."""

import json
import re
from pathlib import Path
from typing import cast

import deadlift.cli.main
from deadlift.cli.app import policy_text

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skills" / "setup-deadlift" / "SKILL.md"


def usages() -> dict[str, str]:
    """Each command of the CLI's usage, such as ``deadlift app add``, and its usage line."""
    usage = deadlift.cli.main.__doc__ or ""
    found: dict[str, str] = {}
    command = ""
    for line in usage.split("\n"):
        matched = re.match(r"^  (deadlift (?:server|app) [a-z]+|deadlift init)\b", line)
        if matched:
            command = matched[1]
            found[command] = line
        elif command and line.startswith("    "):
            # A usage line goes on onto lines indented further.
            found[command] += line
        else:
            command = ""
    return found


def named_commands() -> list[str]:
    """Every deadlift command the skill names: in a code span, or on a line of a sh block."""
    text = SKILL.read_text(encoding="utf-8")
    blocks = re.findall(r"```sh\n(.*?)```", text, re.S)
    run = [line for block in blocks for line in block.split("\n") if line.startswith("deadlift ")]
    spans = [span for span in re.findall(r"`([^`\n]+)`", text) if span.startswith("deadlift ")]
    return run + spans


def test_every_deadlift_command_it_runs_is_one_the_cli_has_with_its_options() -> None:
    known = usages()
    named = named_commands()
    assert len(named) >= 6
    for command in named:
        name = next((c for c in known if command == c or command.startswith(f"{c} ")), None)
        assert name is not None, f"{command!r} isn't a deadlift command"
        for option in re.findall(r"--[a-z-]+", command):
            assert option in known[name], f"{command!r}: {name} has no {option}"


def policy_tests(app: str) -> dict[str, list[dict[str, object]]]:
    """The policy tests the skill has the agent write, filled in for ``app`` alone."""
    text = SKILL.read_text(encoding="utf-8")
    block = re.search(r"```hujson\n(.*?)```", text, re.S)
    assert block is not None
    lines = [line.strip() for line in block[1].split("\n")]
    hujson = "\n".join(line for line in lines if not line.startswith("//"))
    plain = re.sub(r",(\s*[\]}])", r"\1", hujson.replace("<app>", app))
    plain = plain.replace('"root", "deadlift-<other>"', '"root"')
    return cast(dict[str, list[dict[str, object]]], json.loads(plain))


def test_its_policy_tests_name_the_tag_and_user_app_add_prints() -> None:
    printed = policy_text("shop", "acme/shop")
    entries = json.loads(printed[printed.index("{") : printed.rindex("}") + 1])
    (rule,) = entries["ssh"]

    tests = policy_tests("shop")

    (network,) = tests["tests"]
    (ssh,) = tests["sshTests"]
    assert network["src"] == rule["src"][0]
    assert ssh["src"] == rule["src"][0]
    assert ssh["dst"] == rule["dst"]
    assert ssh["accept"] == rule["users"]
    assert "root" in cast(list[str], ssh["deny"])
    assert network["accept"] == [f"{rule['dst'][0]}:22"]
