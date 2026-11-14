"""Matchers for assertions that check part of a value, such as only some of a mapping's keys."""

from collections.abc import Mapping
from typing import override


class Containing:
    """Equal to any mapping that has these keys with equal values, whatever else it has.

    `assert run.outputs == Containing({"outcome": "skipped"})`, and inside lists and other
    matchers: `assert used == [Containing({"operation": "deploy"})]`.
    """

    def __init__(self, expected: Mapping[str, object]) -> None:
        self.expected = dict(expected)

    @override
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Mapping):
            return False
        actual: Mapping[object, object] = other  # pyright: ignore[reportUnknownVariableType]
        return all(key in actual and actual[key] == v for key, v in self.expected.items())

    @override
    def __hash__(self) -> int:
        return id(self)

    @override
    def __repr__(self) -> str:
        return f"Containing({self.expected!r})"
