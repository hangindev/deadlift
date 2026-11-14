"""The test runner's GitHub Actions expressions (tests/support/expressions.py), checked against
what GitHub's documentation says they do:
https://docs.github.com/en/actions/reference/workflows-and-actions/expressions
"""

import pytest

from tests.support.expressions import Scope, Value, condition, evaluate, interpolate

OK = Scope(contexts={}, success=True, failure=False, cancelled=False)


def scope(**contexts: Value) -> Scope:
    return Scope(contexts=contexts, success=True, failure=False, cancelled=False)


def test_literals() -> None:
    assert evaluate("'It''s open source!'", OK) == "It's open source!"
    assert evaluate("711", OK) == 711
    assert evaluate("2.5", OK) == 2.5
    assert evaluate("true", OK) is True
    assert evaluate("false", OK) is False
    assert evaluate("null", OK) is None


def test_contexts_are_read_by_property_or_index_whatever_the_case() -> None:
    s = scope(github={"event": {"Number": 42, "labels": [{"name": "a"}, {"name": "b"}]}})
    assert evaluate("github.event.number", s) == 42
    assert evaluate("GitHub['event']['number']", s) == 42
    assert evaluate("github.event.labels[1].name", s) == "b"


def test_a_missing_property_is_null() -> None:
    s = scope(github={"event": {}})
    assert evaluate("github.event.nothing.here", s) is None
    assert evaluate("github.event.labels[3]", s) is None


FRUITS: Value = [
    {"name": "apple", "quantity": 1},
    {"name": "orange", "quantity": 2},
    {"name": "pear"},
]


def test_an_object_filter_picks_a_property_of_each_item() -> None:
    s = scope(fruits=FRUITS)
    assert evaluate("fruits.*.name", s) == ["apple", "orange", "pear"]
    assert evaluate("fruits[*].quantity", s) == [1, 2]
    assert evaluate("fruits.*.colour", s) == []


def test_an_object_filter_over_an_object_takes_its_values() -> None:
    s = scope(steps={"a": {"outcome": "success"}, "b": {"outcome": "failure"}})
    assert evaluate("steps.*.outcome", s) == ["success", "failure"]


def test_equality_ignores_case_and_compares_other_types_as_numbers() -> None:
    assert evaluate("'Main' == 'main'", OK) is True
    assert evaluate("'main' != 'MAIN'", OK) is False
    assert evaluate("null == 0", OK) is True
    assert evaluate("true == 1", OK) is True
    assert evaluate("'' == 0", OK) is True
    assert evaluate("' 42 ' == 42", OK) is True
    assert evaluate("'abc' == 0", OK) is False
    assert evaluate("'abc' != 'abd'", OK) is True


def test_objects_are_equal_only_to_themselves() -> None:
    s = scope(a={"x": 1}, b={"x": 1})
    assert evaluate("a == a", s) is True
    assert evaluate("a == b", s) is False


def test_ordering() -> None:
    assert evaluate("2 < 10", OK) is True
    assert evaluate("'2' < 10", OK) is True
    assert evaluate("'b' > 'A'", OK) is True
    assert evaluate("1 >= 1", OK) is True
    assert evaluate("2 <= 1", OK) is False


def test_logic_gives_an_operand_and_not_gives_a_boolean() -> None:
    s = scope(github={"ref": "refs/heads/main"})
    pick = "github.ref == 'refs/heads/main' && 'production' || 'staging'"
    assert evaluate(pick, s) == "production"
    assert evaluate("github.ref == 'x' && 'production' || 'staging'", s) == "staging"
    assert evaluate("'' || null", OK) is None
    assert evaluate("!''", OK) is True
    assert evaluate("!github.ref", s) is False
    assert evaluate("!(1 == 2) && !0", OK) is True


LABELS = scope(github={"event": {"issue": {"labels": [{"name": "bug"}, {"name": "help wanted"}]}}})


def test_contains_looks_in_a_string_or_an_array() -> None:
    assert evaluate("contains('Hello world', 'LLO')", OK) is True
    assert evaluate("contains(github.event.issue.labels.*.name, 'Bug')", LABELS) is True
    assert evaluate("contains(github.event.issue.labels.*.name, 'bu')", LABELS) is False


def test_starts_with_and_ends_with_ignore_case() -> None:
    assert evaluate("startsWith('Hello world', 'he')", OK) is True
    assert evaluate("endswith('Hello world', 'LD')", OK) is True
    assert evaluate("startsWith('Hello world', 'world')", OK) is False


def test_format_and_join() -> None:
    hello = "format('Hello {0} {1} {2}', 'Mona', 'the', 'Octocat')"
    assert evaluate(hello, OK) == "Hello Mona the Octocat"
    assert evaluate("join(github.event.issue.labels.*.name, ', ')", LABELS) == "bug, help wanted"
    assert evaluate("join(github.event.issue.labels.*.name)", LABELS) == "bug,help wanted"
    assert evaluate("join('one', ', ')", OK) == "one"


def test_to_json_and_from_json() -> None:
    assert evaluate("toJSON(a)", scope(a={"x": [1, True]})) == (
        '{\n  "x": [\n    1,\n    true\n  ]\n}'
    )
    assert evaluate("fromJSON('{\"x\": [1, null]}').x[0]", OK) == 1
    assert evaluate("fromJSON('true')", OK) is True


def test_the_status_functions_say_how_the_job_is_going() -> None:
    failing = Scope(contexts={}, success=False, failure=True, cancelled=False)
    assert evaluate("success()", failing) is False
    assert evaluate("failure()", failing) is True
    assert evaluate("cancelled()", failing) is False
    assert evaluate("always()", failing) is True


def test_interpolation_puts_each_value_in_the_text() -> None:
    s = scope(github={"actor": "Mona"}, a={"x": 1}, n=None)
    text = "Hello ${{ github.actor }}: ${{ 1 == 1 }} ${{ a }} [${{ n }}] ${{\n  2.0 }}"
    assert interpolate(text, s) == 'Hello Mona: true {"x":1} [] 2'


FAILING = Scope(contexts={"a": "yes"}, success=False, failure=True, cancelled=False)


def test_a_condition_without_a_status_function_needs_success() -> None:
    assert condition("a == 'yes'", scope(a="yes")) is True
    assert condition("a == 'yes'", FAILING) is False
    assert condition("${{ a == 'yes' }}", FAILING) is False


def test_a_condition_with_a_status_function_is_taken_as_written() -> None:
    assert condition("${{ always() }}", FAILING) is True
    assert condition("failure()", FAILING) is True
    assert condition("!cancelled() && a == 'yes'", FAILING) is True
    assert condition("success()", FAILING) is False


@pytest.mark.parametrize(
    ("source", "why"),
    [
        ("1 2", "unexpected 2"),
        ("a ~ b", "can't read the expression"),
        ("(1 == 1", "expected \\)"),
        ("1 ==", "ends too soon"),
        ("nothing.here", "no context named nothing"),
    ],
)
def test_a_malformed_expression_is_an_error(source: str, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        evaluate(source, scope(a=1, b=2))


def test_an_unknown_function_is_an_error() -> None:
    with pytest.raises(ValueError, match="no function hashFiles"):
        evaluate("hashFiles('x')", OK)
