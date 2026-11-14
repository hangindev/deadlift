"""The Sweep: destroying every Feature Environment of an App that isn't in a list to keep, on its
own or first thing in a Feature deploy."""

from collections.abc import Sequence

import pytest

from tests.support.harness import (
    Delivery,
    Harness,
    ServeMapping,
    Server,
    get,
    matches,
    put_note,
)

pytestmark = pytest.mark.usefixtures("remove_servers_after_each")


def deploy(
    harness: Harness,
    server: Server,
    key: str,
    extra: Sequence[str] = (),
    app: str | None = None,
) -> Delivery:
    """Delivers the example App to the Feature Environment `key` of `app` (App a's by
    default)."""
    return server.deliver(
        app if app is not None else server.apps["a"].name,
        ["deploy", f"feature-{key}", "--image", f"web={harness.image()}", *extra],
    )


def sweep(server: Server, *args: str) -> Delivery:
    return server.deliver(server.apps["a"].name, ["sweep", *args])


class TestSweepKeepKeys:
    def test_destroys_every_feature_environment_not_listed_and_reports_the_swept_keys(
        self, harness: Harness
    ) -> None:
        server = harness.server(slots=3)
        app = server.apps["a"].name
        deploy(harness, server, "first")
        deploy(harness, server, "second")
        deploy(harness, server, "third")

        result = sweep(server, "--keep", "second,other")

        assert result.code == 0, result.output
        assert result.outcome == "done"
        assert result.swept == "first,third"
        assert matches(r"^deadlift: swept feature-first and feature-third", result.reason)
        assert matches(r"Swept: first, third", result.summary)
        containers = server.containers(app)
        assert not matches(r"feature-first|feature-third", containers)
        assert get(server.slot_port("a", 2), "/version") == "1"
        assert server.serve_mappings() == [
            ServeMapping(f"svc:{app}", 10002, f"http://127.0.0.1:{server.slot_port('a', 2)}")
        ]

    def test_is_done_sweeping_nothing_when_every_feature_environment_is_kept(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        deploy(harness, server, "first")

        result = sweep(server, "--keep", "first")

        assert result.code == 0, result.output
        assert result.outcome == "done"
        assert result.swept == ""
        assert matches(r"^deadlift: swept nothing", result.reason)
        assert get(server.slot_port("a", 1), "/version") == "1"

    @pytest.mark.parametrize("key", ["Upper", "under_score", "a" * 31])
    def test_refuses_a_kept_key_that_isnt_one_exit_2_sweeping_nothing(
        self, harness: Harness, key: str
    ) -> None:
        server = harness.server()
        deploy(harness, server, "first")

        result = sweep(server, "--keep", f"other,{key}")

        assert result.code == 2, result.output
        assert matches(r"^deadlift: .*key", result.reason)
        assert get(server.slot_port("a", 1), "/version") == "1"


class TestTheGuardOnAnEmptyKeepList:
    @pytest.mark.parametrize(
        "args",
        [[], ["--keep", ""], ["--keep", ",,"]],
        ids=["no list at all", "an empty --keep", "a --keep of only commas"],
    )
    def test_refuses_exit_2_sweeping_nothing(self, harness: Harness, args: list[str]) -> None:
        server = harness.server()
        deploy(harness, server, "first")

        result = sweep(server, *args)

        assert result.code == 2, result.output
        assert result.outcome == "failed"
        assert matches(r"^deadlift: .*--keep-none", result.reason)
        assert result.swept == ""
        assert get(server.slot_port("a", 1), "/version") == "1"

    def test_keep_none_destroys_every_feature_environment_of_the_app(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        app = server.apps["a"].name
        deploy(harness, server, "first")
        deploy(harness, server, "second")

        result = sweep(server, "--keep-none")

        assert result.code == 0, result.output
        assert result.swept == "first,second"
        assert server.containers(app) == ""
        assert server.serve_mappings() == []

    def test_refuses_both_keep_and_keep_none_exit_2_sweeping_nothing(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        deploy(harness, server, "first")

        result = sweep(server, "--keep", "other", "--keep-none")

        assert result.code == 2, result.output
        assert get(server.slot_port("a", 1), "/version") == "1"


class TestDeployFeatureKeyKeepKeys:
    def test_sweeps_unlisted_feature_environments_first_so_a_full_set_of_stale_ones_doesnt_skip_it(
        self, harness: Harness
    ) -> None:
        server = harness.server(slots=2)
        app = server.apps["a"].name
        deploy(harness, server, "stale")
        deploy(harness, server, "open")

        result = deploy(harness, server, "new", ["--keep", "open,new"])

        assert result.code == 0, result.output
        assert result.outcome == "done"
        assert result.environment == "feature-new"
        assert result.swept == "stale"
        assert matches(r"^deadlift: deployed feature-new.*swept feature-stale", result.reason)
        # The stale one's Slot, at its address.
        assert result.url == f"https://{app}.tailtest.ts.net:10001/"
        assert get(server.slot_port("a", 1), "/version") == "1"
        assert get(server.slot_port("a", 2), "/version") == "1"
        assert not matches(r"feature-stale", server.containers(app))

    def test_always_keeps_the_environment_it_deploys_even_when_it_isnt_listed(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        deploy(harness, server, "first")
        deploy(harness, server, "second")

        result = deploy(harness, server, "second", ["--keep-none"])

        assert result.outcome == "done", result.output
        assert result.swept == "first"
        assert matches(r":10002/\Z", result.url)
        assert get(server.slot_port("a", 2), "/version") == "1"

    def test_is_skipped_sweeping_nothing_when_every_slot_is_held_by_kept_keys(
        self, harness: Harness
    ) -> None:
        server = harness.server(slots=1)
        deploy(harness, server, "first")

        result = deploy(harness, server, "second", ["--keep", "first"])

        assert result.outcome == "skipped", result.output
        assert result.swept == ""
        assert get(server.slot_port("a", 1), "/version") == "1"

    def test_sweeps_nothing_when_the_template_is_refused(self, harness: Harness) -> None:
        server = harness.server()
        deploy(harness, server, "stale")

        result = server.deliver(
            server.apps["a"].name,
            ["deploy", "feature-new", "--image", f"web={harness.image()}", "--keep", "new"],
            template="services:\n  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n",
        )

        assert result.code == 2, result.output
        assert result.swept == ""
        assert get(server.slot_port("a", 1), "/version") == "1"

    def test_refuses_an_empty_keep_exit_2_sweeping_and_deploying_nothing(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        app = server.apps["a"].name
        deploy(harness, server, "stale")

        result = deploy(harness, server, "new", ["--keep", ""])

        assert result.code == 2, result.output
        assert matches(r"^deadlift: .*--keep-none", result.reason)
        assert not matches(r"feature-new", server.containers(app))
        assert get(server.slot_port("a", 1), "/version") == "1"


class TestWhatASweepNeverTouches:
    def test_the_dev_environment_or_its_data(self, harness: Harness) -> None:
        server = harness.server()
        app = server.apps["a"].name
        dev = server.deliver(app, ["deploy", "dev", "--image", f"web={harness.image()}"])
        assert dev.outcome == "done", dev.output
        put_note(server.dev_port("a"), "kept")
        deploy(harness, server, "first")

        result = sweep(server, "--keep-none")

        assert result.outcome == "done", result.output
        assert result.swept == "first"
        assert get(server.dev_port("a"), "/version") == "1"
        assert get(server.dev_port("a"), "/note") == "kept"
        assert server.serve_mappings() == [
            ServeMapping(f"svc:{app}", 443, f"http://127.0.0.1:{server.dev_port('a')}")
        ]

    def test_deploy_dev_refuses_keep_and_keep_none_exit_2_sweeping_nothing(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        deploy(harness, server, "first")

        for keep in (["--keep", "other"], ["--keep-none"]):
            result = server.deliver(
                server.apps["a"].name,
                ["deploy", "dev", "--image", f"web={harness.image()}", *keep],
            )

            assert result.code == 2, result.output
            assert matches(r"^deadlift: .*Feature deploy", result.reason)
        assert get(server.slot_port("a", 1), "/version") == "1"

    def test_another_apps_feature_environments_even_with_keys_the_app_doesnt_keep(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        b = server.apps["b"].name
        deploy(harness, server, "first")
        deploy(harness, server, "first", app=b)
        deploy(harness, server, "second", app=b)

        result = sweep(server, "--keep-none")

        assert result.swept == "first"
        assert get(server.slot_port("b", 1), "/version") == "1"
        assert get(server.slot_port("b", 2), "/version") == "1"
        assert [f"{m.service}:{m.port}" for m in server.serve_mappings()] == [
            f"svc:{b}:10001",
            f"svc:{b}:10002",
        ]
