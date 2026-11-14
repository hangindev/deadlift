"""The maintainer's ``deadlift app``: its checks on names and arguments, and what it asks of the
Tailscale API, apart from SSH and HTTP."""

import json

import pytest

from deadlift.cli.app import (
    AppArgs,
    app_policy,
    parse_app_args,
    service_matches,
    service_ports,
)
from deadlift.cli.common import Usage, is_server_name
from deadlift.core.names import app_name_problem, is_app_name


@pytest.mark.parametrize(
    "app", ["shop", "a", "a1", "my-shop", "a" * 23, "featureful", "shop-features", "x-featur"]
)
def test_an_app_name_takes_up_to_23_of_a_to_z_0_to_9_and_dashes(app: str) -> None:
    assert is_app_name(app)
    assert app_name_problem(app) is None


@pytest.mark.parametrize(
    "app", ["", "Shop", "1shop", "shop-", "a" * 24, "sh_op", "shop\n", "shöp", "-shop"]
)
def test_an_app_name_refuses_anything_else(app: str) -> None:
    assert not is_app_name(app)
    assert app_name_problem(app) == (
        f"'{app}' isn't an App name: up to 23 of a-z, 0-9 and -, starting with a letter and not"
        " ending with -"
    )


@pytest.mark.parametrize("app", ["shop-feature", "shop-feature-x", "a-feature-feature"])
def test_an_app_name_refuses_a_feature_part_that_could_clash(app: str) -> None:
    assert not is_app_name(app)
    assert app_name_problem(app) == (
        f"'{app}' isn't an App name: a part named feature after a - could clash with another"
        " App's Feature Environments"
    )


@pytest.mark.parametrize(
    "server", ["s", "deadlift-1", "203.0.113.7", "a.tail1.ts.net", "fd7a:115c::1"]
)
def test_a_server_is_a_host_name_or_address(server: str) -> None:
    assert is_server_name(server)


@pytest.mark.parametrize("server", ["", "-s", "bad host", "user@s", ".s", "s\n", "s/x"])
def test_a_server_is_nothing_else(server: str) -> None:
    assert not is_server_name(server)


def test_app_arguments_split_into_the_server_positionals_and_options() -> None:
    args = parse_app_args(["shop", "dev", "--server", "s", "--slots", "2", "--yes", "f"])

    assert args == AppArgs("shop", "s", ["dev", "f"], ["--slots", "2", "--yes"])


def test_app_arguments_take_the_last_server() -> None:
    assert parse_app_args(["shop", "--server", "a", "--server", "b"]).server == "b"


@pytest.mark.parametrize(
    ("argv", "why"),
    [
        ([], "'' isn't an App name"),
        (["--server", "s"], "'--server' isn't an App name"),
        (["shop"], "--server is required"),
        (["shop", "--server"], "--server needs a value"),
        (["shop", "--server", ""], "--server needs a value"),
        (["shop", "--server", "s", "--repo"], "--repo needs a value"),
        (["shop", "--server", "bad host"], "'bad host' isn't a host name or address"),
    ],
)
def test_app_arguments_refuse_bad_usage(argv: list[str], why: str) -> None:
    with pytest.raises(Usage) as refused:
        parse_app_args(argv)

    assert refused.value.why.startswith(why)
    assert refused.value.usage.startswith("Usage: deadlift server provision\n")


def test_a_service_lists_443_and_each_slots_port() -> None:
    assert service_ports(1) == ["tcp:443", "tcp:10001"]
    assert service_ports(3) == ["tcp:443", "tcp:10001", "tcp:10002", "tcp:10003"]


def test_a_service_matches_with_its_ports_in_any_order() -> None:
    body = json.dumps(
        {"name": "svc:shop", "ports": ["tcp:10001", "tcp:443"], "tags": ["tag:deadlift-svc"]}
    )

    assert service_matches(body, ["tcp:443", "tcp:10001"])


@pytest.mark.parametrize(
    "body",
    [
        '{"ports": ["tcp:443"], "tags": ["tag:deadlift-svc"]}',
        '{"ports": ["tcp:443", "tcp:10001"], "tags": ["tag:other"]}',
        '{"ports": ["tcp:443", "tcp:10001"], "tags": ["tag:deadlift-svc", "tag:x"]}',
        '{"tags": ["tag:deadlift-svc"]}',
        '{"ports": null, "tags": ["tag:deadlift-svc"]}',
        '{"ports": ["tcp:443", 1], "tags": ["tag:deadlift-svc"]}',
        '["tcp:443"]',
        "not json",
        "",
    ],
)
def test_a_service_doesnt_match_otherwise(body: str) -> None:
    assert not service_matches(body, ["tcp:443", "tcp:10001"])


def test_an_apps_policy_entries_let_its_ci_reach_only_ssh_as_its_deploy_user() -> None:
    assert app_policy("shop") == {
        "tagOwners": {"tag:deadlift-ci-shop": ["autogroup:admin"]},
        "grants": [
            {"src": ["tag:deadlift-ci-shop"], "dst": ["tag:deadlift-server"], "ip": ["tcp:22"]}
        ],
        "ssh": [
            {
                "action": "accept",
                "src": ["tag:deadlift-ci-shop"],
                "dst": ["tag:deadlift-server"],
                "users": ["deadlift-shop"],
            }
        ],
    }
