import io

import pytest

from deadlift.core.outcome import (
    Exit,
    Stop,
    fail,
    output,
    parse_outputs,
    reason,
    refuse,
    run_main,
    skip,
)


def test_exit_codes_are_deadlifts() -> None:
    assert [int(code) for code in Exit] == [0, 1, 2, 75]
    assert Exit(75) is Exit.SKIPPED


def test_a_reason_line_starts_with_deadlift_and_goes_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    reason("deployed feature-42.")
    assert capsys.readouterr() == ("", "deadlift: deployed feature-42.\n")


def test_an_output_is_a_line_of_its_own_on_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    output("url", "https://example.tail1234.ts.net:10001/")
    assert capsys.readouterr() == (
        "deadlift-output: url=https://example.tail1234.ts.net:10001/\n",
        "",
    )


@pytest.mark.parametrize("value", ["two\nlines", "a\rb"])
def test_an_output_never_spans_lines(value: str) -> None:
    with pytest.raises(ValueError, match="span lines"):
        output("reason", value, to=io.StringIO())


def test_an_output_name_is_plain() -> None:
    with pytest.raises(ValueError, match="output name"):
        output("a=b", "c", to=io.StringIO())


def test_outputs_are_read_back_from_other_lines() -> None:
    lines = [
        "Waiting for another operation on this Server to finish",
        "deadlift-output: swept=7,9\n",
        "deadlift: swept feature-7 and feature-9.",
        "deadlift-output: url=https://a.b/?x=1",
        "deadlift-output: swept=",
        "deadlift-output: no equals sign",
    ]
    assert parse_outputs(lines) == {"swept": "", "url": "https://a.b/?x=1"}


@pytest.mark.parametrize(
    ("stop", "code"),
    [(refuse, Exit.REFUSED), (fail, Exit.FAILED), (skip, Exit.SKIPPED)],
)
def test_stopping_prints_the_reason_and_exits_with_its_code(
    capsys: pytest.CaptureFixture[str], stop: object, code: Exit
) -> None:
    def body() -> None:
        assert callable(stop)
        stop("no free Slot")

    assert run_main(body) == code
    assert capsys.readouterr().err == "deadlift: no free Slot\n"


def test_a_stop_without_a_reason_prints_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    def body() -> None:
        raise Stop(Exit.REFUSED, None)

    assert run_main(body) == 2
    assert capsys.readouterr() == ("", "")


def test_a_body_that_returns_gives_its_code() -> None:
    assert run_main(lambda: None) == 0
    assert run_main(lambda: Exit.SKIPPED) == 75
