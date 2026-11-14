import shlex

import pytest

from deadlift.actions.deliver import Report, remote_command, report, summary


def test_the_remote_command_is_the_entry_command_with_the_operation_word_for_word() -> None:
    words = ["deploy", "feature-42", "--image", "web=x:1", "", "it's; rm -rf /", "$(id)\n"]
    command = remote_command(words)
    assert shlex.split(command) == [
        "sudo",
        "--non-interactive",
        "/usr/local/sbin/deadlift-entry",
        "--protocol",
        "1",
        *words,
    ]


def test_a_delivery_that_exits_0_is_done() -> None:
    log = "deadlift-output: url=https://a/\nstarting\ndeadlift: deployed dev\n"
    assert report(0, log, server="s", app="a") == Report(
        "done", "deadlift: deployed dev", url="https://a/", environment="", swept="", status=0
    )


def test_a_delivery_that_exits_75_is_skipped_and_passes() -> None:
    assert report(75, "deadlift: not deployed, no Slot\n", server="s", app="a") == Report(
        "skipped", "deadlift: not deployed, no Slot", status=0
    )


@pytest.mark.parametrize("status", [1, 2, 137])
def test_any_other_exit_fails_with_the_servers_code(status: int) -> None:
    assert report(status, "Killed\n", server="s", app="a") == Report("failed", "", status=status)


def test_an_unreachable_server_fails_with_1_saying_so() -> None:
    assert report(255, "ssh: connect timed out\n", server="deadlift-1", app="shop") == Report(
        "failed",
        "deadlift: couldn't reach deadlift-1 over SSH as deadlift-shop, so nothing was run.",
        status=1,
    )


def test_the_last_reason_and_the_last_of_each_output_count() -> None:
    log = (
        "deadlift: first\n"
        "deadlift-output: environment=feature-1\n"
        "deadlift-output: swept=3,4\n"
        "  deadlift: not a reason line\n"
        "deadlift-output: environment=feature-2\n"
        "deadlift: last"
    )
    assert report(0, log, server="s", app="a") == Report(
        "done", "deadlift: last", environment="feature-2", swept="3,4", status=0
    )


def test_the_summary_says_what_was_done_where_and_what_was_swept() -> None:
    done = Report("done", "deadlift: deployed feature-42", url="https://x/", swept="7,9", status=0)
    assert summary("deploy", ["feature-42", "--image", "web=x"], done) == [
        "### deadlift deploy feature-42: done",
        "",
        "> deadlift: deployed feature-42",
        "",
        "- Address: https://x/",
        "- Swept: 7, 9",
    ]


def test_the_summary_of_an_operation_with_no_environment_names_only_the_operation() -> None:
    swept = Report("done", "deadlift: swept nothing", status=0)
    assert summary("sweep", ["--keep", "1"], swept) == [
        "### deadlift sweep: done",
        "",
        "> deadlift: swept nothing",
    ]


def test_the_summary_says_when_the_server_gave_no_reason() -> None:
    assert summary("destroy", ["feature-42"], Report("failed", "", status=1)) == [
        "### deadlift destroy feature-42: failed",
        "",
        "> The Server gave no reason. The step's log has the details.",
    ]
