"""The Dev Environment: an App's CI delivers each merge to it, at the root of the App's Service.

A Volume Snapshot is taken before each deploy, and Recovery puts it back, with the previous
version, when a deploy fails after Dev stopped.
"""

import re

import pytest

from tests.support.harness import (
    Delivery,
    Harness,
    ImageKind,
    ServeMapping,
    Server,
    docker,
    example_template,
    get,
    matches,
    put_note,
)

pytestmark = pytest.mark.usefixtures("remove_servers_after_each")


def deploy_dev(harness: Harness, server: Server, kind: ImageKind, version: int) -> Delivery:
    """Deploys the example App's `kind` image at `version` to app a's Dev Environment."""
    return server.deliver(
        server.apps["a"].name, ["deploy", "dev", "--image", f"web={harness.image(kind, version)}"]
    )


def dev_on_version_1(harness: Harness, *, snapshots: int | None = None) -> Server:
    """A Server whose app a has Dev up on version 1 of the example App, holding a note."""
    server = harness.server(snapshots=snapshots)
    v1 = deploy_dev(harness, server, "healthy", 1)
    assert v1.outcome == "done", v1.output
    put_note(server.dev_port("a"), "written on v1")
    return server


class TestDeployDev:
    def test_brings_the_example_app_up_healthy_at_the_root_of_the_apps_service_and_deliver_reports_its_url(  # noqa: E501
        self, harness: Harness
    ) -> None:
        server = harness.server()
        app = server.apps["a"].name

        result = deploy_dev(harness, server, "healthy", 1)

        assert result.code == 0, result.output
        assert result.outcome == "done"
        assert result.environment == "dev"
        assert result.url == f"https://{app}.tailtest.ts.net/"
        assert matches(r"^deadlift: ", result.reason)
        assert get(server.dev_port("a"), "/version") == "1"
        assert server.serve_mappings() == [
            ServeMapping(f"svc:{app}", 443, f"http://127.0.0.1:{server.dev_port('a')}")
        ]

    def test_replaces_the_running_version_with_the_new_one_keeping_devs_data(
        self, harness: Harness
    ) -> None:
        server = dev_on_version_1(harness)

        result = deploy_dev(harness, server, "healthy", 2)

        assert result.outcome == "done", result.output
        assert get(server.dev_port("a"), "/version") == "2"
        assert get(server.dev_port("a"), "/note") == "written on v1"

    def test_leaves_dev_running_and_unchanged_when_an_image_cant_be_pulled(
        self, harness: Harness
    ) -> None:
        server = dev_on_version_1(harness)
        before = server.started_at(server.apps["a"].name, "dev", "web")
        missing = re.sub(r":[^:/]+$", ":no-such-tag", harness.image("healthy", 1), count=1)

        result = server.deliver(
            server.apps["a"].name, ["deploy", "dev", "--image", f"web={missing}"]
        )

        assert result.code == 1, result.output
        assert result.outcome == "failed"
        assert matches(r"^deadlift: .*no-such-tag.*Nothing was changed", result.reason)
        assert server.started_at(server.apps["a"].name, "dev", "web") == before
        assert get(server.dev_port("a"), "/version") == "1"

    def test_leaves_dev_running_and_unchanged_when_the_template_is_refused(
        self, harness: Harness
    ) -> None:
        server = dev_on_version_1(harness)
        before = server.started_at(server.apps["a"].name, "dev", "web")
        template = re.sub(r"^x-deadlift:\n.*\n", "", example_template(), count=1, flags=re.M)

        result = server.deliver(
            server.apps["a"].name,
            ["deploy", "dev", "--image", f"web={harness.image('healthy', 2)}"],
            template=template,
        )

        assert result.code == 2, result.output
        assert matches(r"x-deadlift\.route", result.reason)
        assert server.started_at(server.apps["a"].name, "dev", "web") == before
        assert get(server.dev_port("a"), "/version") == "1"

    def test_keeps_the_apps_configured_number_of_volume_snapshots(self, harness: Harness) -> None:
        server = dev_on_version_1(harness, snapshots=2)

        for version in (2, 3, 4):
            assert deploy_dev(harness, server, "healthy", version).outcome == "done"

        assert len(server.volume_snapshots(server.apps["a"].name)) == 2

    def test_keeps_the_volume_snapshots_bounded_however_many_deploys_fail(
        self, harness: Harness
    ) -> None:
        server = dev_on_version_1(harness, snapshots=2)

        for version in (2, 3, 4, 5):
            result = deploy_dev(harness, server, "failing-migration", version)
            assert matches(r"Recovery succeeded", result.reason), result.output

        # The 2 newest, and the one Dev's current version was first deployed over, which
        # --discard-newer-data goes back to.
        assert len(server.volume_snapshots(server.apps["a"].name)) == 3

    def test_keeps_3_volume_snapshots_when_the_app_doesnt_say(self, harness: Harness) -> None:
        server = dev_on_version_1(harness)

        for version in (2, 3, 4):
            assert deploy_dev(harness, server, "healthy", version).outcome == "done"

        assert len(server.volume_snapshots(server.apps["a"].name)) == 3


class TestRecovery:
    def test_puts_back_the_previous_version_and_its_data_when_a_migration_fails_after_dev_stopped(
        self, harness: Harness
    ) -> None:
        server = dev_on_version_1(harness)

        result = deploy_dev(harness, server, "failing-migration", 2)

        assert result.code == 1, result.output
        assert result.outcome == "failed"
        assert matches(r"^deadlift: dev ", result.reason)
        assert matches(r"migrate", result.reason)
        assert matches(r"Recovery succeeded", result.reason)
        assert get(server.dev_port("a"), "/version") == "1"
        assert get(server.dev_port("a"), "/note") == "written on v1"

    def test_puts_back_the_data_as_it_was_before_the_deploy_which_the_previous_version_needs(
        self, harness: Harness
    ) -> None:
        server = dev_on_version_1(harness)

        # Version 2 migrates the data to its schema, then never reports healthy. Version 1
        # refuses data migrated past its own schema, so it only starts again on the data from
        # before.
        result = deploy_dev(harness, server, "unhealthy", 2)

        assert result.code == 1, result.output
        assert matches(r"unhealthy", result.reason)
        assert matches(r"Recovery succeeded", result.reason)
        assert get(server.dev_port("a"), "/version") == "1"
        assert get(server.dev_port("a"), "/note") == "written on v1"

    def test_leaves_dev_stopped_saying_so_when_the_first_deploy_fails(
        self, harness: Harness
    ) -> None:
        server = harness.server()

        result = deploy_dev(harness, server, "failing-migration", 1)

        assert result.code == 1, result.output
        assert matches(r"^deadlift: dev .*no previous version.*Dev is stopped", result.reason)
        assert server.running_containers(server.apps["a"].name) == ""

    def test_says_recovery_failed_and_that_dev_is_stopped_when_the_previous_version_doesnt_start_either(  # noqa: E501
        self, harness: Harness
    ) -> None:
        server = harness.server()
        previous = harness.image("healthy", 9)
        assert deploy_dev(harness, server, "healthy", 9).outcome == "done"
        # Gone from the Server, so Recovery can't start it again.
        docker("image", "rm", "--force", previous)

        result = deploy_dev(harness, server, "failing-migration", 10)

        assert result.code == 1, result.output
        assert matches(r"^deadlift: dev .*Recovery failed.*Dev is stopped", result.reason)
        assert server.running_containers(server.apps["a"].name) == ""
