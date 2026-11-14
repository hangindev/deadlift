"""Rollback: the Dev Environment goes back one version on request.

It keeps today's data when the previous version is healthy on it, and discards it, keeping a
Volume Snapshot of it first, only when told to with --discard-newer-data.
"""

import pytest

from tests.support.harness import (
    Delivery,
    Harness,
    Server,
    docker,
    example_template,
    get,
    matches,
    put_note,
)

pytestmark = pytest.mark.usefixtures("remove_servers_after_each")


def template_for_release(release: str) -> str:
    """The example App's template with `RELEASE` set on its web service, so a test can tell two
    versions of the same image apart through `/env/RELEASE`."""
    return example_template().replace(
        "  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n",
        "  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n    environment:\n"
        f'      RELEASE: "{release}"\n',
        1,
    )


def deploy_dev(
    harness: Harness, server: Server, version: int, *, template: str | None = None
) -> None:
    """Deploys version `version` of the example App's healthy image to app a's Dev
    Environment."""
    result = server.deliver(
        server.apps["a"].name,
        ["deploy", "dev", "--image", f"web={harness.image('healthy', version)}"],
        template=template,
    )
    assert result.outcome == "done", result.output


def rollback(server: Server, *args: str) -> Delivery:
    return server.deliver(server.apps["a"].name, ["rollback", *args])


class TestRollbackDev:
    def test_starts_the_previous_version_on_todays_data_and_keeps_it_when_its_healthy(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        app = server.apps["a"].name
        # Two releases of the same image: the previous one is healthy on today's data.
        deploy_dev(harness, server, 1, template=template_for_release("first"))
        deploy_dev(harness, server, 1, template=template_for_release("second"))
        put_note(server.dev_port("a"), "written today")

        result = rollback(server, "dev")

        assert result.code == 0, result.output
        assert result.outcome == "done"
        assert result.environment == "dev"
        assert result.url == f"https://{app}.tailtest.ts.net/"
        assert matches(r"^deadlift: rolled dev back", result.reason)
        assert get(server.dev_port("a"), "/env/RELEASE") == "first"
        assert get(server.dev_port("a"), "/note") == "written today"

    def test_is_refused_naming_discard_newer_data_and_starts_the_current_version_again_when_the_previous_one_isnt_healthy_on_todays_data(  # noqa: E501
        self, harness: Harness
    ) -> None:
        server = harness.server()
        deploy_dev(harness, server, 1)
        # Version 2 migrates the data to its schema, which version 1's migration refuses.
        deploy_dev(harness, server, 2)
        put_note(server.dev_port("a"), "written on v2")

        result = rollback(server, "dev")

        assert result.code == 2, result.output
        assert result.outcome == "failed"
        assert matches(r"^deadlift: .*--discard-newer-data", result.reason)
        assert get(server.dev_port("a"), "/version") == "2"
        assert get(server.dev_port("a"), "/note") == "written on v2"

    def test_with_discard_newer_data_keeps_a_volume_snapshot_of_todays_data_then_starts_the_previous_version_on_the_data_from_before_the_current_one(  # noqa: E501
        self, harness: Harness
    ) -> None:
        server = harness.server()
        app = server.apps["a"].name
        deploy_dev(harness, server, 1)
        put_note(server.dev_port("a"), "written on v1")
        deploy_dev(harness, server, 2)
        put_note(server.dev_port("a"), "written on v2")
        before = server.volume_snapshots(app)

        result = rollback(server, "dev", "--discard-newer-data")

        assert result.code == 0, result.output
        assert result.outcome == "done"
        assert matches(r"^deadlift: rolled dev back", result.reason)
        assert get(server.dev_port("a"), "/version") == "1"
        assert get(server.dev_port("a"), "/note") == "written on v1"
        taken = [name for name in server.volume_snapshots(app) if name not in before]
        assert len(taken) == 1
        assert server.file_in_volume_snapshot(app, taken[0], "note") == "written on v2"
        assert result.reason is not None
        assert taken[0] in result.reason

    def test_pulls_the_previous_versions_images_when_the_server_no_longer_has_them(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        deploy_dev(harness, server, 1)
        put_note(server.dev_port("a"), "written on v1")
        deploy_dev(harness, server, 2)
        docker("image", "rm", "--force", harness.image("healthy", 1))

        result = rollback(server, "dev", "--discard-newer-data")

        assert result.outcome == "done", result.output
        assert get(server.dev_port("a"), "/version") == "1"

    def test_goes_back_one_step_only(self, harness: Harness) -> None:
        server = harness.server()
        deploy_dev(harness, server, 1, template=template_for_release("first"))
        deploy_dev(harness, server, 1, template=template_for_release("second"))
        deploy_dev(harness, server, 1, template=template_for_release("third"))
        assert rollback(server, "dev").outcome == "done"
        before = server.started_at(server.apps["a"].name, "dev", "web")

        result = rollback(server, "dev")

        assert result.code == 2, result.output
        assert matches(r"^deadlift: .*no previous version", result.reason)
        assert get(server.dev_port("a"), "/env/RELEASE") == "second"
        assert server.started_at(server.apps["a"].name, "dev", "web") == before


def test_rollback_is_refused_for_a_feature_environment(harness: Harness) -> None:
    server = harness.server()
    app = server.apps["a"].name
    deployed = server.deliver(app, ["deploy", "feature-42", "--image", f"web={harness.image()}"])
    assert deployed.outcome == "done", deployed.output
    before = server.started_at(app, "feature-42", "web")

    result = rollback(server, "feature-42")

    assert result.code == 2, result.output
    assert matches(r"^deadlift: .*only .*Dev Environment", result.reason)
    assert server.started_at(app, "feature-42", "web") == before
