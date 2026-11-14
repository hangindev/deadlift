"""The ask-deadlift skill tells an agent what deadlift printed and why, so what it quotes must be
what deadlift prints: every reason line, every exit code and every command it names.

A reason line is quoted as a code span starting `deadlift: `, with `<placeholder>` or `…` where
the line carries something of the run's own (an Environment, a URL, Compose's last line). The
rest of it must be text deadlift's code puts there, joined as the code joins it.
"""

import ast
import re
from functools import cache
from pathlib import Path

import pytest

import deadlift.cli.main
from deadlift.actions.deliver import report
from deadlift.core.outcome import Exit

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skills" / "ask-deadlift"
SOURCE = REPO / "src" / "deadlift"

_SPAN = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_PLACEHOLDER = re.compile(r"<[^<>]+>|…")


def skill_files() -> list[Path]:
    return sorted(SKILL.glob("*.md"))


def code_spans(text: str) -> list[str]:
    """Markdown's inline code spans, as their contents (one space either side dropped when both
    are there, as Markdown drops it)."""
    spans: list[str] = []
    for matched in _SPAN.finditer(text):
        span = matched[2]
        if len(span) > 2 and span.startswith(" ") and span.endswith(" "):
            span = span[1:-1]
        spans.append(span)
    return spans


def quoted(prefix: str) -> list[tuple[str, str]]:
    """Every code span in the skill starting with ``prefix``, with the file it's in."""
    return [
        (path.name, span)
        for path in skill_files()
        for span in code_spans(path.read_text())
        if span.startswith(prefix)
    ]


@cache
def pieces() -> frozenset[str]:
    """The literal text of every string deadlift's code builds: each constant, and each run of
    text between an f-string's fields, with implicit concatenation already joined."""
    found: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(), str(path))
        inside_fstrings: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                for value in node.values:
                    inside_fstrings.add(id(value))
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        found.add(value.value)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in inside_fstrings
            ):
                found.add(node.value)
    return frozenset(piece for piece in found if piece)


def joined_from_pieces(text: str) -> bool:
    """Whether ``text`` is deadlift's own text: inside one piece, or the end of one piece, then
    whole pieces, then the start of one, as a field or an outer message joins them."""
    every = pieces()
    if any(text in piece for piece in every):
        return True

    @cache
    def rest(remaining: str) -> bool:
        if not remaining:
            return True
        return any(piece.startswith(remaining) for piece in every) or any(
            remaining.startswith(piece) and rest(remaining[len(piece) :]) for piece in every
        )

    return any(
        rest(text[len(piece[-k:]) :])
        for piece in every
        for k in range(1, min(len(piece), len(text)) + 1)
        if text.startswith(piece[-k:])
    )


def test_the_skill_quotes_reason_lines() -> None:
    assert len(quoted("deadlift: ")) >= 20


@pytest.mark.parametrize(("file", "line"), quoted("deadlift: "), ids=lambda value: str(value)[:70])
def test_every_reason_line_it_quotes_is_one_deadlift_prints(file: str, line: str) -> None:
    for part in _PLACEHOLDER.split(line):
        assert joined_from_pieces(part), f"{file}: deadlift never prints {part!r} in {line!r}"


def exit_code_rows() -> list[list[str]]:
    """The rows of SKILL.md's exit code table: a row whose first cell is a code span holding a
    number."""
    rows: list[list[str]] = []
    for line in (SKILL / "SKILL.md").read_text().split("\n"):
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("|") and re.fullmatch(r"`[0-9]+`", cells[0]):
            rows.append([cell.strip("`") for cell in cells])
    return rows


def test_its_exit_codes_are_deadlifts() -> None:
    meanings = {int(row[0]): re.findall(r"[a-z]+", row[1])[0] for row in exit_code_rows()}
    assert meanings == {int(code): code.name.lower() for code in Exit}


@pytest.mark.parametrize("row", exit_code_rows(), ids=lambda row: row[0])
def test_each_exit_codes_outcome_and_step_are_what_deliver_reports(row: list[str]) -> None:
    code, _, outcome, step = row[:4]
    delivered = report(int(code), "deadlift: something\n", server="s", app="a")
    assert delivered.outcome == outcome
    assert step == ("passes" if delivered.status == 0 else "fails")


def test_every_deadlift_command_it_names_is_one_the_cli_has() -> None:
    usage = deadlift.cli.main.__doc__ or ""
    commands = set(re.findall(r"^  (deadlift [a-z]+(?: [a-z]+)?)", usage, flags=re.M))
    named = quoted("deadlift ")
    assert named
    for file, command in named:
        assert any(command == c or command.startswith(f"{c} ") for c in commands), (
            f"{file}: {command!r} isn't a deadlift command"
        )
