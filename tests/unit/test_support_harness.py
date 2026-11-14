"""The delivery tests' harness: the helpers that don't need Docker."""

import time

import pytest

from tests.support.harness import TimeLimitExceeded, parse_github_output, shell_quote, time_limit


def test_a_github_output_holds_name_value_lines() -> None:
    assert parse_github_output("outcome=done\nurl=https://a.example/?q=1\n") == {
        "outcome": "done",
        "url": "https://a.example/?q=1",
    }


def test_a_github_output_holds_heredoc_values_over_several_lines() -> None:
    text = "reason<<EOF_1\ndeadlift: one\nx=y\nEOF_1\nswept=\n"

    assert parse_github_output(text) == {"reason": "deadlift: one\nx=y", "swept": ""}


def test_a_github_output_ignores_lines_that_are_neither() -> None:
    assert parse_github_output("\nnothing here\noutcome=skipped") == {"outcome": "skipped"}


@pytest.mark.parametrize(
    ("word", "quoted"),
    [
        ("deploy", "deploy"),
        ("web=127.0.0.1:5000/x/example:v1-healthy", "web=127.0.0.1:5000/x/example:v1-healthy"),
        ("../a/feature-1", "../a/feature-1"),
        ("", "''"),
        ("two words", "'two words'"),
        ("it's", "'it'\\''s'"),
        ("$(touch x)", "'$(touch x)'"),
    ],
)
def test_a_word_is_quoted_for_the_remote_shell_only_when_it_needs_it(
    word: str, quoted: str
) -> None:
    assert shell_quote(word) == quoted


def test_a_time_limit_stops_what_runs_past_it() -> None:
    started = time.monotonic()

    with pytest.raises(TimeLimitExceeded, match=r"0\.2 s"), time_limit(0.2):
        time.sleep(5)

    assert time.monotonic() - started < 2


def test_a_time_limit_lets_what_finishes_in_time_finish_and_is_then_off() -> None:
    with time_limit(0.2):
        pass

    time.sleep(0.4)
