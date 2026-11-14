"""Feature Environments sharing an App's Slots: redeploying a key, running out of Slots, and
destroying one to free its Slot."""

import pytest

from tests.support.harness import (
    Delivery,
    Harness,
    ImageKind,
    ServeMapping,
    Server,
    get,
    matches,
)

pytestmark = pytest.mark.usefixtures("remove_servers_after_each")


def deploy(
    harness: Harness, server: Server, key: str, version: int = 1, kind: ImageKind = "healthy"
) -> Delivery:
    """Delivers the example App, at `version`, to the Feature Environment `key` of App a."""
    return server.deliver(
        server.apps["a"].name,
        ["deploy", f"feature-{key}", "--image", f"web={harness.image(kind, version)}"],
    )


def destroy(server: Server, key: str) -> Delivery:
    return server.deliver(server.apps["a"].name, ["destroy", f"feature-{key}"])


class TestSlots:
    def test_each_feature_environment_takes_a_free_slot_with_its_loopback_and_public_ports(
        self, harness: Harness
    ) -> None:
        server = harness.server(slots=3)
        app = server.apps["a"].name

        first = deploy(harness, server, "first", 1)
        second = deploy(harness, server, "second", 2)

        assert first.outcome == "done", first.output
        assert second.outcome == "done", second.output
        assert first.url == f"https://{app}.tailtest.ts.net:10001/"
        assert second.url == f"https://{app}.tailtest.ts.net:10002/"
        assert get(server.slot_port("a", 1), "/version") == "1"
        assert get(server.slot_port("a", 2), "/version") == "2"
        mappings = server.serve_mappings()
        assert ServeMapping(
            f"svc:{app}", 10001, f"http://127.0.0.1:{server.slot_port('a', 1)}"
        ) in (mappings)
        assert ServeMapping(
            f"svc:{app}", 10002, f"http://127.0.0.1:{server.slot_port('a', 2)}"
        ) in (mappings)

    def test_redeploying_a_key_keeps_its_slot_its_url_and_its_data(self, harness: Harness) -> None:
        server = harness.server(slots=3)
        deploy(harness, server, "first", 1)
        first = deploy(harness, server, "second", 1)

        redeployed = deploy(harness, server, "second", 2)

        assert redeployed.outcome == "done", redeployed.output
        assert redeployed.url == first.url
        assert get(server.slot_port("a", 2), "/version") == "2"
        # The data now has schema 2, which version 1's migration refuses: so it was kept.
        back = deploy(harness, server, "second", 1)
        assert back.outcome == "failed", back.output
        assert matches(r"migrate", back.reason)

    def test_a_failed_redeploy_leaves_that_environment_stopped_and_the_apps_others_running(
        self, harness: Harness
    ) -> None:
        server = harness.server(slots=3)
        app = server.apps["a"].name
        deploy(harness, server, "first", 1)
        deploy(harness, server, "second", 1)

        result = deploy(harness, server, "second", 1, "unhealthy")

        assert result.code == 1, result.output
        assert matches(r"^deadlift: .*feature-second.*stopped", result.reason)
        running = server.running_containers(app)
        assert not matches(r"feature-second", running)
        assert matches(r"feature-first", running)

    def test_with_every_slot_held_a_new_key_is_skipped_naming_the_holders_and_nothing_is_created(
        self, harness: Harness
    ) -> None:
        server = harness.server(slots=2)
        app = server.apps["a"].name
        deploy(harness, server, "first")
        deploy(harness, server, "second")
        mappings = server.serve_mappings()

        result = deploy(harness, server, "third")

        assert result.code == 0, result.output
        assert result.outcome == "skipped"
        assert matches(
            r"^deadlift: .*held, by feature-first and feature-second, so feature-third wasn't "
            r"deployed",
            result.reason,
        )
        assert not matches(r"feature-third", server.containers(app))
        assert server.serve_mappings() == mappings


class TestDestroyFeatureKey:
    def test_removes_the_environment_and_its_service_mapping_and_frees_its_slot(
        self, harness: Harness
    ) -> None:
        server = harness.server(slots=2)
        app = server.apps["a"].name
        deploy(harness, server, "first", 1)
        deploy(harness, server, "second", 2)

        result = destroy(server, "first")

        assert result.code == 0, result.output
        assert result.outcome == "done"
        assert result.environment == "feature-first"
        assert matches(r"^deadlift: destroyed feature-first", result.reason)
        assert not matches(r"feature-first", server.containers(app))
        assert server.serve_mappings() == [
            ServeMapping(f"svc:{app}", 10002, f"http://127.0.0.1:{server.slot_port('a', 2)}")
        ]
        # Its Slot is free again for a new key, at the same address.
        third = deploy(harness, server, "third", 1)
        assert third.outcome == "done", third.output
        assert third.url == f"https://{app}.tailtest.ts.net:10001/"
        assert get(server.slot_port("a", 2), "/version") == "2"

    def test_removes_its_volumes_so_the_key_deploys_again_on_fresh_data(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        deploy(harness, server, "first", 2)

        destroy(server, "first")
        # Version 1's migration refuses data with schema 2, so this passes only on fresh data.
        again = deploy(harness, server, "first", 1)

        assert again.outcome == "done", again.output
        assert get(server.slot_port("a", 1), "/version") == "1"

    def test_removes_an_environment_whose_deploy_failed_which_has_no_service_mapping(
        self, harness: Harness
    ) -> None:
        server = harness.server(slots=1)
        app = server.apps["a"].name
        deploy(harness, server, "first", 1, "failing-migration")

        result = destroy(server, "first")

        assert result.outcome == "done", result.output
        assert server.containers(app) == ""
        assert deploy(harness, server, "second").outcome == "done"

    def test_is_done_changing_nothing_for_a_key_that_isnt_deployed(self, harness: Harness) -> None:
        server = harness.server()
        deploy(harness, server, "first")
        mappings = server.serve_mappings()

        result = destroy(server, "other")

        assert result.code == 0, result.output
        assert result.outcome == "done"
        assert matches(r"^deadlift: .*feature-other.*nothing to destroy", result.reason)
        assert server.serve_mappings() == mappings
        assert get(server.slot_port("a", 1), "/version") == "1"

    def test_leaves_another_apps_environment_with_the_same_key_alone(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        b = server.apps["b"].name
        deploy(harness, server, "first")
        server.deliver(b, ["deploy", "feature-first", "--image", f"web={harness.image()}"])

        destroy(server, "first")

        assert get(server.slot_port("b", 1), "/version") == "1"
        assert server.serve_mappings() == [
            ServeMapping(f"svc:{b}", 10001, f"http://127.0.0.1:{server.slot_port('b', 1)}")
        ]

    def test_refuses_the_dev_environment_exit_2(self, harness: Harness) -> None:
        server = harness.server()

        result = server.deliver(server.apps["a"].name, ["destroy", "dev"])

        assert result.code == 2, result.output
        assert matches(r"^deadlift: ", result.reason)


KEYS = ["Upper", "under_score", "dot.ted", "-leading", "", "a" * 31]


class TestAKeyThatIsntUpTo30OfAZ09AndDash:
    @pytest.mark.parametrize("key", KEYS)
    def test_is_refused_by_deploy_exit_2_creating_nothing(self, harness: Harness, key: str) -> None:
        server = harness.server()
        app = server.apps["a"].name

        result = deploy(harness, server, key)

        assert result.code == 2, result.output
        assert matches(r"^deadlift: .*key", result.reason)
        assert server.containers(app) == ""
        assert server.serve_mappings() == []

    @pytest.mark.parametrize("key", KEYS)
    def test_is_refused_by_destroy_exit_2(self, harness: Harness, key: str) -> None:
        server = harness.server()
        deploy(harness, server, "first")

        result = destroy(server, key)

        assert result.code == 2, result.output
        assert matches(r"^deadlift: .*key", result.reason)
        assert get(server.slot_port("a", 1), "/version") == "1"

    def test_is_accepted_at_30_characters(self, harness: Harness) -> None:
        server = harness.server()

        result = deploy(harness, server, "a" * 30)

        assert result.outcome == "done", result.output
