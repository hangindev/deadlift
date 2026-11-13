"""Which part of deadlift may import which, and what each may need beyond the standard library.

Each part runs somewhere different, with no uv and no pip: the Server has the standard library
and PyYAML (python3-yaml), bootstrap runs before PyYAML is installed, and the CLI and the actions
run on whatever python3 the maintainer's computer or the runner has (ADR 0004).
"""

import ast
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "src" / "deadlift"

# What each part may import: other deadlift parts, and modules outside the standard library.
ALLOWED: dict[str, tuple[set[str], set[str]]] = {
    "core": ({"core"}, set()),
    "bootstrap": ({"core", "bootstrap"}, set()),
    "server": ({"core", "server"}, {"yaml"}),
    "cli": ({"core", "cli"}, set()),
    "actions": ({"core", "actions"}, set()),
}


def imports(path: Path) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                found.append((node.lineno, "." * node.level + (node.module or "")))
            elif node.module:
                found.append((node.lineno, node.module))
    return found


def modules() -> list[Path]:
    return sorted(SOURCE.rglob("*.py"))


def test_every_module_is_in_a_part() -> None:
    for path in modules():
        parts = path.relative_to(SOURCE).parts
        assert parts == ("__init__.py",) or parts[0] in ALLOWED, f"{path} is in no part"


@pytest.mark.parametrize("path", modules(), ids=lambda path: str(path.relative_to(SOURCE)))
def test_a_module_imports_only_what_its_part_may(path: Path) -> None:
    parts = path.relative_to(SOURCE).parts
    deadlift_parts, third_party = ALLOWED.get(parts[0], (set(), set()))
    for line, name in imports(path):
        where = f"{path.relative_to(SOURCE)}:{line} imports {name}"
        assert not name.startswith("."), f"{where}: use absolute imports"
        top = name.split(".")[0]
        if top == "deadlift":
            assert name.split(".")[1] in deadlift_parts if "." in name else False, where
        else:
            assert top in sys.stdlib_module_names or top in third_party, where
