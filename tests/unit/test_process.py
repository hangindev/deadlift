import os
import sys
from pathlib import Path

import pytest

from deadlift.core.process import (
    NOT_FOUND,
    SAFE_PATH,
    Env,
    Stream,
    run,
    scrubbed_env,
    start,
)


def environment_of_child(env: Env) -> dict[str, str]:
    done = run(["/usr/bin/env"], env=env, stdout=Stream.CAPTURE)
    assert done.ok
    return dict(line.split("=", 1) for line in done.stdout.splitlines())


def test_a_child_gets_only_the_environment_it_is_given() -> None:
    env = scrubbed_env(
        source={"PATH": "/usr/bin:/bin", "HOME": "/root", "API_KEY": "from the caller"},
        set_vars={"DEADLIFT_APP": "example"},
    )
    assert environment_of_child(env) == {
        "PATH": "/usr/bin:/bin",
        "HOME": "/root",
        "DEADLIFT_APP": "example",
    }


def test_this_process_environment_is_not_passed_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEADLIFT_LEAK", "yes")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/evil.so")
    child = environment_of_child(scrubbed_env())
    assert set(child) <= {"PATH", "HOME"}
    assert child["PATH"] == os.environ["PATH"]


def test_a_child_has_a_safe_path_when_none_is_kept() -> None:
    assert environment_of_child(scrubbed_env(keep=())) == {"PATH": SAFE_PATH}


def test_named_variables_are_kept_and_set_ones_win() -> None:
    env = scrubbed_env(
        keep=("PATH", "DOCKER_HOST"),
        source={"PATH": "/bin", "DOCKER_HOST": "unix:///x.sock", "HOME": "/home/x"},
        set_vars={"PATH": "/usr/bin"},
    )
    assert env.variables == {"PATH": "/usr/bin", "DOCKER_HOST": "unix:///x.sock"}
    assert env.with_vars({"A": "1"}).variables["A"] == "1"


@pytest.mark.parametrize("name", ["", "A=B", "A\0"])
def test_an_environment_holds_only_real_variables(name: str) -> None:
    with pytest.raises(ValueError, match="environment variable"):
        Env({name: "x"})


def test_an_inheritable_fd_of_ours_does_not_reach_a_child() -> None:
    read_end, write_end = os.pipe()
    try:
        os.set_inheritable(write_end, True)
        inode = os.fstat(write_end).st_ino
        # Whatever the child's fd of that number is, if any, it isn't our pipe.
        done = run(
            ["/bin/sh", "-c", f"stat -L -c %i /proc/$$/fd/{write_end} 2>/dev/null || echo none"],
            env=scrubbed_env(),
            stdout=Stream.CAPTURE,
        )
        assert done.stdout.strip() != str(inode)
    finally:
        os.close(read_end)
        os.close(write_end)


def test_a_child_reads_dev_null_unless_given_input() -> None:
    env = scrubbed_env()
    assert run(["cat"], env=env, stdout=Stream.CAPTURE).stdout == ""
    assert run(["cat"], env=env, input="delivery", stdout=Stream.CAPTURE).stdout == "delivery"
    with pytest.raises(ValueError, match="not both"):
        run(["cat"], env=env, input="x", stdin=Stream.INHERIT)


def test_a_child_stdout_goes_to_our_stderr_by_default(capfd: pytest.CaptureFixture[str]) -> None:
    assert run(["echo", "pulled"], env=scrubbed_env()).ok
    assert capfd.readouterr() == ("", "pulled\n")


def test_a_child_can_write_to_a_file_of_ours(tmp_path: Path) -> None:
    path = tmp_path / "out"
    with path.open("wb") as file:
        assert run(["printf", "tar"], env=scrubbed_env(), stdout=file).ok
    assert path.read_bytes() == b"tar"


def test_a_missing_program_is_code_127_as_in_a_shell() -> None:
    done = run(["deadlift-no-such-program"], env=scrubbed_env(), stderr=Stream.CAPTURE)
    assert (done.code, done.ok) == (NOT_FOUND, False)
    assert "deadlift-no-such-program" in done.stderr


def test_exit_codes_and_captured_streams() -> None:
    done = run(
        ["/bin/sh", "-c", "echo out; echo err >&2; exit 3"],
        env=scrubbed_env(),
        stdout=Stream.CAPTURE,
        stderr=Stream.CAPTURE,
    )
    assert (done.code, done.stdout, done.stderr) == (3, "out\n", "err\n")


def test_a_command_line_string_is_refused() -> None:
    with pytest.raises(TypeError, match="not a command line"):
        run("echo hi", env=scrubbed_env())


def test_start_streams_one_child_into_another() -> None:
    env = scrubbed_env()
    producer = start(["printf", "a\nb\n"], env=env, stdout=Stream.CAPTURE)
    assert producer.stdout is not None
    consumer = run(["wc", "-l"], env=env, stdin=producer.stdout, stdout=Stream.CAPTURE)
    producer.stdout.close()
    assert producer.wait() == 0
    assert consumer.stdout.strip() == "2"


def test_python_children_run_isolated_when_asked() -> None:
    # The shape every privileged entry point uses: the system python3, -I, a scrubbed env.
    done = run(
        [sys.executable, "-I", "-c", "import sys; print(sys.flags.isolated)"],
        env=scrubbed_env(),
        stdout=Stream.CAPTURE,
    )
    assert done.stdout.strip() == "1"


def test_a_child_killed_by_a_signal_exits_as_a_shell_reports_it() -> None:
    done = run(["sh", "-c", "kill -TERM $$"], env=scrubbed_env())
    assert done.code == 128 + 15
