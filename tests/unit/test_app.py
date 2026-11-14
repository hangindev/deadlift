"""The Server half of `deadlift app`: the rules it checks before changing anything."""

import pytest

from deadlift.server import app


@pytest.mark.parametrize("name", ["a", "shop", "my-shop2", "a" * 23, "feature-x", "x-features"])
def test_takes_an_app_name_deadlift_can_use(name: str) -> None:
    assert app.app_name_problem(name) is None


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("", "isn't an App name: up to 23 of a-z"),
        ("Shop", "isn't an App name: up to 23 of a-z"),
        ("1shop", "isn't an App name: up to 23 of a-z"),
        ("shop-", "isn't an App name: up to 23 of a-z"),
        ("a" * 24, "isn't an App name: up to 23 of a-z"),
        ("shop\n", "isn't an App name: up to 23 of a-z"),
        ("shop-feature", "a part named feature after a - could clash"),
        ("shop-feature-x", "a part named feature after a - could clash"),
    ],
)
def test_refuses_an_app_name_deadlift_cant_use(name: str, why: str) -> None:
    problem = app.app_name_problem(name)

    assert problem is not None
    assert problem.startswith(f"'{name}' ")
    assert why in problem


@pytest.mark.parametrize(
    ("parts", "joined"),
    [
        (["its sudo rule"], "its sudo rule"),
        (["a", "b"], "a and b"),
        (["a", "b", "c"], "a, b and c"),
        (["a", "b", "c", "d"], "a, b, c and d"),
    ],
)
def test_joins_what_it_did_as_in_a_sentence(parts: list[str], joined: str) -> None:
    assert app.sentence(parts) == joined


def test_gives_a_first_app_the_first_block_of_ports() -> None:
    assert app.free_port_base(set()) == 20000


def test_gives_the_lowest_block_no_other_app_has() -> None:
    assert app.free_port_base({"20000", "20200"}) == 20100


def test_finds_no_block_when_every_one_is_taken() -> None:
    assert app.free_port_base({str(base) for base in range(20000, 30000, 100)}) is None


def test_reads_the_settings_add_is_given() -> None:
    given = ["--slots", "2", "--memory", "256m", "--cpus", "0.5", "--pids", "100", "--slots", "4"]

    assert app.add_settings(given) == {"slots": "4", "memory": "256m", "cpus": "0.5", "pids": "100"}


def test_counts_an_empty_setting_as_not_given() -> None:
    assert app.add_settings(["--slots", "", "--snapshots", "5"]) == {"snapshots": "5"}


@pytest.mark.parametrize(
    ("given", "why"),
    [
        (["--slots"], "--slots needs a value"),
        (["--colour", "red"], "unknown argument '--colour' for add"),
        (["--slots", "10"], "--slots is a number of Slots from 1 to 9"),
        (["--slots", "0"], "--slots is a number of Slots from 1 to 9"),
        (["--snapshots", "0"], "--snapshots is a number of Volume Snapshots to keep, at least 1"),
        (
            ["--snapshots", "1000"],
            "--snapshots is a number of Volume Snapshots to keep, at least 1",
        ),
        (["--memory", "512mb"], "--memory is a size such as 512m or 2g"),
        (["--memory", "0m"], "--memory is a size such as 512m or 2g"),
        (["--cpus", "0.0"], "--cpus is a number of CPUs above 0, such as 0.5 or 2"),
        (["--cpus", ".5"], "--cpus is a number of CPUs above 0, such as 0.5 or 2"),
        (["--pids", "01"], "--pids is a number of processes, at least 1"),
    ],
)
def test_refuses_a_setting_it_cant_use(given: list[str], why: str) -> None:
    with pytest.raises(app.BadUsage, match=f"^{why}$"):
        app.add_settings(given)


def test_reads_the_last_value_of_a_setting() -> None:
    conf = "port_base=20000\nslots=3\nslots=5\nmemory=1g\n"

    assert app.setting(conf, "slots") == "5"
    assert app.setting(conf, "port_base") == "20000"
    assert app.setting(conf, "pids") == ""


@pytest.mark.parametrize(
    ("status", "has"),
    [
        ('{"Services": {"svc:shop": {"TCP": {"443": {"HTTPS": true}}}}}', True),
        ('{"Services": {"svc:shop": {}}}', True),
        ('{"Services": {"svc:other": {}}}', False),
        ('{"Services": {"svc:shop": null}}', False),
        ("{}", False),
        ("", False),
        ("not json", False),
        ("[]", False),
    ],
)
def test_finds_whether_tailscale_serve_maps_a_service(status: str, has: bool) -> None:
    assert app.serve_has_service(status, "svc:shop") is has
