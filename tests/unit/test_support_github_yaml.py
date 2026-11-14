"""The test runner reads workflows as GitHub does, by YAML 1.2's core schema, not PyYAML's 1.1:
https://yaml.org/spec/1.2.2/#1032-tag-resolution
"""

from tests.support.github_yaml import load


def test_on_and_yes_are_strings() -> None:
    assert load("on:\n  push:\nyes: no\noff: y\n") == {
        "on": {"push": None},
        "yes": "no",
        "off": "y",
    }


def test_booleans_are_only_true_and_false() -> None:
    expected = [True, True, True, False, False, False]
    assert load("[true, True, TRUE, false, False, FALSE]") == expected


def test_integers_and_floats() -> None:
    assert load("[12, -3, 012, 0o17, 0x1F, 1:30, 1_000, 2.5, .5, 1e3, .inf]") == [
        12,
        -3,
        12,
        15,
        31,
        "1:30",
        "1_000",
        2.5,
        0.5,
        1000.0,
        float("inf"),
    ]


def test_nulls() -> None:
    assert load("[~, null, Null, NULL]") == [None, None, None, None]
    assert load("a:\n") == {"a": None}
