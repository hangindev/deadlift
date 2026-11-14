"""``deadlift server provision``, the wizard that gets a new Server, driven as a maintainer would:
a scripted human reads what it prints and answers each prompt in turn. The machines it walks them
through buying, and the tailnet they join, are fakes (tests/support/fakes/provision/): ``ssh``
to a machine and the maintainer's own ``tailscale``. Nothing here reaches a real machine or
tailnet, and no browser opens."""

import json
import re
from typing import cast

from tests.support.provision_world import Tailnet, Turn, World


def stages(output: str) -> list[str]:
    """The stage titles a run showed, in order."""
    return [match[1] for match in re.finditer(r"▸ Stage \d+/\d+ · (.*)", output)]


def printed_policy(output: str) -> dict[str, object]:
    """The policy entries printed, parsed: HuJSON is JSON with comments and trailing commas."""
    found = re.search(r"\n\n(    \{\n[\s\S]*?\n    \})\n", output)
    if not found:
        raise AssertionError(f"no policy printed in:\n{output}")
    text = re.sub(r"^\s*//.*$", "", found[1], flags=re.MULTILINE)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return as_object(json.loads(text))


def as_object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def whole_run(w: World, name: str, address: str, policy: Turn) -> list[Turn]:
    """Every turn of a first run, from start to finish, for a Server named ``name`` at
    ``address``."""
    return [
        Turn(r"Ready to start\?"),
        policy,
        Turn(r"such as deadlift-1", answer=name),
        Turn(r"Create & Buy now", answer=address, then=lambda: w.buy(address)),
        Turn(r"Created and applied\?"),
        Turn(r"Bootstrapped\? Continue\?"),
        Turn(r"Copy the Tailnet DNS name", answer="tailtest.ts.net"),
        Turn(r"Continue\?"),
        Turn(r"Deleted\?", then=lambda: w.close_public_ssh(address)),
    ]


def test_walks_through_the_policy_buying_the_provider_firewall_bootstrap_joining_the_tailnet_and_closing_public_ssh(  # noqa: E501
    world: World,
) -> None:
    w = world
    run = w.provision(whole_run(w, "deadlift-1", "203.0.113.7", Turn(r"Saved\?")))

    assert run.code == 0, run.output
    assert stages(run.output) == [
        "Tailscale: the tailnet policy",
        "Hetzner: buy the Server",
        "Hetzner: the firewall",
        "Bootstrap the Server",
        "Tailscale: join the tailnet",
        "Hetzner: close public SSH",
    ]
    assert w.bootstraps("203.0.113.7") == ["public"]
    assert w.tailnet("203.0.113.7") == Tailnet("deadlift-1", ["tag:deadlift-server"])
    assert re.search(r"SSH on 203\.0\.113\.7 no longer answers", run.output)
    assert re.search(r"deadlift app add <app> --server deadlift-1 --repo <owner/name>", run.output)


def test_prints_the_policy_entries_set_once_per_tailnet_covering_every_apps_service_by_its_tag(
    world: World,
) -> None:
    w = world
    run = w.provision(whole_run(w, "deadlift-1", "203.0.113.7", Turn(r"Saved\?")))

    assert run.code == 0, run.output
    assert "https://login.tailscale.com/admin/acls/file" in w.opened()
    policy = printed_policy(run.output)
    assert sorted(as_object(policy["tagOwners"])) == ["tag:deadlift-server", "tag:deadlift-svc"]
    assert policy["autoApprovers"] == {"services": {"tag:deadlift-svc": ["tag:deadlift-server"]}}
    # Dev at 443, and Slots 1 to 9 at 10000 + Slot.
    grants = policy["grants"]
    assert isinstance(grants, list)
    assert {
        "src": ["autogroup:member"],
        "dst": ["tag:deadlift-svc"],
        "ip": ["tcp:443", "tcp:10001-10009"],
    } in grants
    # Nothing per App or per Service: those come from `deadlift app add`.
    assert not re.search(r"svc:|deadlift-ci|deadlift-1", json.dumps(policy))


def test_a_second_server_on_the_same_tailnet_reuses_the_policy_entries_and_asks_for_nothing_new(
    world: World,
) -> None:
    w = world
    first = w.provision(whole_run(w, "deadlift-1", "203.0.113.7", Turn(r"Saved\?")))
    assert first.code == 0, first.output
    opened_before = len(w.opened())

    second = w.provision(
        whole_run(
            w,
            "deadlift-2",
            "203.0.113.8",
            Turn(r"already has a deadlift Server[\s\S]*Continue\?"),
        )
    )

    assert second.code == 0, second.output
    assert re.search(r"nothing to add", second.output)
    assert not re.search(r"autoApprovers|tagOwners|Saved\?", second.output)
    assert "https://login.tailscale.com/admin/acls/file" not in w.opened()[opened_before:]
    assert w.bootstraps("203.0.113.8") == ["public"]
    assert w.tailnet("203.0.113.8") == Tailnet("deadlift-2", ["tag:deadlift-server"])


def test_waits_for_each_steps_confirmation_and_carries_on_with_the_saved_answers_after_ctrl_c(
    world: World,
) -> None:
    w = world

    def nothing_ran_yet() -> None:
        # Nothing is run on the machine while the maintainer is still setting its firewall up.
        assert w.bootstraps("203.0.113.7") == []

    interrupted = w.provision(
        [
            Turn(r"Ready to start\?"),
            Turn(r"Saved\?"),
            Turn(r"such as deadlift-1", answer="deadlift-1"),
            Turn(r"Create & Buy now", answer="203.0.113.7", then=lambda: w.buy("203.0.113.7")),
            Turn(r"Created and applied\?", then=nothing_ran_yet, interrupt=True),
        ]
    )
    assert interrupted.code != 0
    assert w.bootstraps("203.0.113.7") == []

    resumed = w.provision(
        [
            Turn(r"Ready to start\?"),
            Turn(r"earlier run[\s\S]*Continue\?"),
            Turn(r"such as deadlift-1", answer=""),
            Turn(r"Create & Buy now", answer=""),
            Turn(r"Created and applied\?"),
            Turn(r"Bootstrapped\? Continue\?"),
            Turn(r"Copy the Tailnet DNS name", answer=""),
            Turn(r"Continue\?"),
            Turn(r"Deleted\?", then=lambda: w.close_public_ssh("203.0.113.7")),
        ]
    )

    assert resumed.code == 0, resumed.output
    assert not re.search(r"autoApprovers", resumed.output)
    assert w.bootstraps("203.0.113.7") == ["public"]
    assert w.tailnet("203.0.113.7") == Tailnet("deadlift-1", ["tag:deadlift-server"])


def test_resumed_after_joining_the_tailnet_it_bootstraps_over_the_tailnet_and_doesnt_join_again(
    world: World,
) -> None:
    w = world
    interrupted = w.provision(
        [
            *whole_run(w, "deadlift-1", "203.0.113.7", Turn(r"Saved\?"))[:7],
            Turn(
                r"Continue\?",
                interrupt=True,
                then=lambda: w.close_public_ssh("203.0.113.7"),
            ),
        ]
    )
    assert interrupted.code != 0

    resumed = w.provision(
        [
            Turn(r"Ready to start\?"),
            Turn(r"Continue\?"),
            Turn(r"such as deadlift-1", answer=""),
            Turn(r"Create & Buy now", answer=""),
            Turn(r"Continue\?"),
            Turn(r"Bootstrapped\? Continue\?"),
            Turn(r"already answers over the tailnet[\s\S]*Continue\?"),
        ]
    )

    assert resumed.code == 0, resumed.output
    assert not re.search(r"Deleted\?", resumed.output)
    assert re.search(r"SSH on 203\.0\.113\.7 no longer answers", resumed.output)
    assert w.bootstraps("203.0.113.7") == ["public", "tailnet"]


def test_refuses_arguments_starting_nothing(world: World) -> None:
    run = world.provision([], ["deadlift-1"])

    assert run.code == 2
    assert re.search(r"^deadlift: server provision takes no arguments", run.output)
    assert not re.search(r"Ready to start", run.output)


def test_doesnt_take_the_maintainers_word_that_public_ssh_is_closed(world: World) -> None:
    w = world
    run = w.provision(
        [
            *whole_run(w, "deadlift-1", "203.0.113.7", Turn(r"Saved\?"))[:8],
            Turn(r"Deleted\?"),
            Turn(
                r"SSH on 203\.0\.113\.7 still answers[\s\S]*Check again\?",
                answer="y",
                then=lambda: w.close_public_ssh("203.0.113.7"),
            ),
        ]
    )

    assert run.code == 0, run.output
    assert re.search(r"SSH on 203\.0\.113\.7 no longer answers", run.output)
