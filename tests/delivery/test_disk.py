"""A Server under disk pressure keeps its Dev Environments working: over the lower threshold
Feature deploys are skipped, over the upper one the watchdog stops every Feature Environment, and
images no Environment uses are pruned after each deploy."""

import re
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.support.harness import AppKey, Delivery, Harness, ImageKind, Server, get, matches

pytestmark = pytest.mark.usefixtures("remove_servers_after_each")


def deploy(
    harness: Harness,
    server: Server,
    app: AppKey,
    environment: str,
    version: int = 1,
    *,
    kind: ImageKind = "healthy",
) -> Delivery:
    """Delivers the example App's `kind` image at `version` to `environment` of `app`."""
    return server.deliver(
        server.apps[app].name,
        ["deploy", environment, "--image", f"web={harness.image(kind, version)}"],
    )


class TestTheLowerThreshold:
    def test_over_it_a_feature_deploy_is_skipped_with_a_reason_and_a_dev_deploy_still_goes_ahead(
        self, harness: Harness
    ) -> None:
        server = harness.server(disk_used=81)
        app = server.apps["a"].name

        feature = deploy(harness, server, "a", "feature-first")
        dev = deploy(harness, server, "a", "dev")

        assert feature.outcome == "skipped", feature.output
        assert matches(
            r"^deadlift: the disk is 81% full, over .*80%.*feature-first wasn't deployed",
            feature.reason,
        )
        assert not matches(r"feature-first", server.containers(app))
        assert dev.outcome == "done", dev.output
        assert get(server.dev_port("a"), "/version") == "1"

    def test_is_the_server_wide_setting_and_a_disk_exactly_at_it_isnt_over_it(
        self, harness: Harness
    ) -> None:
        server = harness.server(
            disk_used=70,
            settings="# deadlift's Server-wide settings\nfeature_deploy_disk_limit=70\n"
            "watchdog_disk_limit=90\n",
        )

        at = deploy(harness, server, "a", "feature-first")
        server.set_disk_used(71)
        over = deploy(harness, server, "b", "feature-second")

        assert at.outcome == "done", at.output
        assert over.outcome == "skipped", over.output
        assert matches(r"71% full, over .*70%", over.reason)


class TestTheWatchdog:
    def test_over_the_upper_threshold_stops_every_feature_environment_on_the_server_and_leaves_every_dev_environment_running(  # noqa: E501
        self, harness: Harness
    ) -> None:
        server = harness.server()
        for app in ("a", "b"):
            for environment in ("dev", "feature-first"):
                result = deploy(harness, server, app, environment)
                assert result.outcome == "done", result.output
        server.set_disk_used(91)

        result = server.watchdog()

        assert result.code == 0, result.output
        reasons = [line for line in result.output.split("\n") if line.startswith("deadlift: ")]
        assert matches(r"^deadlift: the disk is 91% full, over .*90%.*stopped", reasons[-1])
        for app in ("a", "b"):
            running = server.running_containers(server.apps[app].name)
            assert matches(r"-dev-web-", running)
            assert not matches(r"feature-first", running)
        assert get(server.dev_port("a"), "/version") == "1"
        assert get(server.dev_port("b"), "/version") == "1"

    def test_a_stopped_feature_environment_starts_again_on_its_next_deploy_at_the_same_address(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        first = deploy(harness, server, "a", "feature-first")
        server.set_disk_used(95)
        server.watchdog()
        server.set_disk_used(50)

        again = deploy(harness, server, "a", "feature-first", 2)

        assert again.outcome == "done", again.output
        assert again.url == first.url
        assert get(server.slot_port("a", 1), "/version") == "2"

    def test_at_or_under_the_upper_threshold_stops_nothing(self, harness: Harness) -> None:
        server = harness.server(settings="feature_deploy_disk_limit=95\nwatchdog_disk_limit=96\n")
        deploy(harness, server, "a", "feature-first")
        server.set_disk_used(96)

        result = server.watchdog()

        assert result.code == 0, result.output
        assert matches(r"^deadlift: the disk is 96% full.*nothing was stopped", result.output, re.M)
        assert matches(r"feature-first", server.running_containers(server.apps["a"].name))

    def test_waits_for_the_operation_in_progress_on_the_server(self, harness: Harness) -> None:
        server = harness.server()
        release = server.hold_lock()

        with ThreadPoolExecutor(1) as pool:
            watchdog = pool.submit(server.watchdog)
            time.sleep(1)
            assert not watchdog.done()
            release()
            result = watchdog.result()

        assert result.code == 0, result.output


class TestPruningImagesAfterEachDeploy:
    def test_dev_keeps_its_current_and_previous_versions_images_and_older_ones_are_removed(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        for version in (1, 2, 3):
            result = deploy(harness, server, "a", "dev", version)
            assert result.outcome == "done", result.output

        assert server.has_image(harness.image("healthy", 1)) is False
        assert server.has_image(harness.image("healthy", 2)) is True
        assert server.has_image(harness.image("healthy", 3)) is True

    def test_an_image_stays_while_any_environment_of_any_app_uses_it_and_goes_after_the_last_one(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        deploy(harness, server, "a", "feature-first", 1)
        deploy(harness, server, "b", "dev", 1)
        for version in (2, 3):
            deploy(harness, server, "a", "dev", version)
        v1 = harness.image("healthy", 1)
        assert server.has_image(v1) is True

        server.deliver(server.apps["a"].name, ["destroy", "feature-first"])
        for version in (2, 3):
            deploy(harness, server, "b", "dev", version)

        assert server.has_image(v1) is False

    def test_a_failed_dev_deploys_image_is_removed_and_devs_running_version_keeps_its_own(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        deploy(harness, server, "a", "dev", 1)

        failed = deploy(harness, server, "a", "dev", 2, kind="unhealthy")

        assert failed.outcome == "failed", failed.output
        assert matches(r"Recovery succeeded", failed.reason)
        assert server.has_image(harness.image("unhealthy", 2)) is False
        assert server.has_image(harness.image("healthy", 1)) is True
        assert get(server.dev_port("a"), "/version") == "1"
