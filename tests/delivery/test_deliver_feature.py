"""The tracer bullet: an App's CI delivers the example App to a Feature Environment."""

import re

import pytest

from tests.support.harness import Harness, ImageKind, ServeMapping, example_template, get, matches

pytestmark = pytest.mark.usefixtures("remove_servers_after_each")


class TestDeployFeatureKey:
    def test_brings_the_example_app_up_healthy_on_its_slot_and_deliver_reports_done_with_its_url(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        app = server.apps["a"].name

        result = server.deliver(
            app, ["deploy", "feature-42", "--image", f"web={harness.image('healthy', 1)}"]
        )

        assert result.code == 0, result.output
        assert result.outcome == "done"
        assert result.environment == "feature-42"
        # The first Slot's public port on the App's own Service name (ADR 0003).
        assert result.url == f"https://{app}.tailtest.ts.net:10001/"
        assert matches(r"^deadlift: ", result.reason)
        assert get(server.slot_port("a", 1), "/version") == "1"

    def test_explains_itself_in_the_job_summary(self, harness: Harness) -> None:
        server = harness.server()
        app = server.apps["a"].name

        result = server.deliver(app, ["deploy", "feature-42", "--image", f"web={harness.image()}"])

        assert "deadlift deploy feature-42: done" in result.summary
        assert result.reason is not None
        assert result.reason in result.summary
        assert f"Address: https://{app}.tailtest.ts.net:10001/" in result.summary

    def test_maps_the_apps_service_to_the_environments_loopback_port(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        app = server.apps["a"].name

        server.deliver(app, ["deploy", "feature-42", "--image", f"web={harness.image()}"])

        assert server.serve_mappings() == [
            ServeMapping(f"svc:{app}", 10001, f"http://127.0.0.1:{server.slot_port('a', 1)}")
        ]

    def test_gives_each_named_image_to_the_template_as_deadlift_image_name(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        app = server.apps["a"].name
        # The routed service runs the image named `site`, and its migration the one named
        # `migrator`.
        template = re.sub(
            r"(\n {2}web:\n {4}image: )\$\{DEADLIFT_IMAGE_WEB\}",
            lambda match: match.group(1) + "${DEADLIFT_IMAGE_SITE}",
            example_template(),
            count=1,
        ).replace("${DEADLIFT_IMAGE_WEB}", "${DEADLIFT_IMAGE_MIGRATOR}", 1)

        result = server.deliver(
            app,
            [
                "deploy",
                "feature-42",
                "--image",
                f"migrator={harness.image('healthy', 1)}",
                "--image",
                f"site={harness.image('healthy', 2)}",
            ],
            template=template,
        )

        assert result.outcome == "done", result.output
        assert get(server.slot_port("a", 1), "/version") == "2"

    def test_goes_ahead_when_a_one_shot_service_that_nothing_depends_on_exits_0(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        app = server.apps["a"].name
        # `docker compose up --wait` over the whole project fails on this one, so deadlift waits
        # on the routed service only.
        template = example_template().replace(
            "services:\n",
            "services:\n  seed:\n    image: ${DEADLIFT_IMAGE_WEB}\n"
            '    command: ["true"]\n    restart: "no"\n',
            1,
        )

        result = server.deliver(
            app, ["deploy", "feature-42", "--image", f"web={harness.image()}"], template=template
        )

        assert result.outcome == "done", result.output
        assert get(server.slot_port("a", 1), "/version") == "1"

    @pytest.mark.parametrize(
        ("kind", "why"),
        [("failing-migration", r"migrate"), ("unhealthy", r"unhealthy")],
        ids=["failing-migration", "unhealthy"],
    )
    def test_fails_exit_1_with_a_kind_image_and_leaves_the_environment_stopped(
        self, harness: Harness, kind: ImageKind, why: str
    ) -> None:
        server = harness.server()
        app = server.apps["a"].name

        result = server.deliver(
            app, ["deploy", "feature-42", "--image", f"web={harness.image(kind)}"]
        )

        assert result.code == 1, result.output
        assert result.outcome == "failed"
        assert matches(r"^deadlift: .*feature-42", result.reason)
        assert matches(why, result.reason)
        assert matches(r"stopped", result.reason)
        assert server.running_containers(app) == ""
