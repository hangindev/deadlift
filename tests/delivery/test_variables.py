"""Variables reaching an App's containers: its template gets the App's secrets file and
deadlift's own variables through `${VAR}` substitution, and nothing else."""

import pytest

from tests.support.harness import Harness, example_template, get, matches

pytestmark = pytest.mark.usefixtures("remove_servers_after_each")


def template_with_environment(environment: dict[str, str]) -> str:
    """The example App's template, with its routed service given `environment`, so the
    container's variables can be read back at `/env/<NAME>`."""
    lines = "".join(f'      {name}: "{value}"\n' for name, value in environment.items())
    return example_template().replace(
        "  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n",
        f"  web:\n    image: ${{DEADLIFT_IMAGE_WEB}}\n    environment:\n{lines}",
        1,
    )


def test_variables_from_the_apps_feature_env_reach_its_feature_environments(
    harness: Harness,
) -> None:
    server = harness.server()
    app = server.apps["a"].name
    server.secrets(app, "feature", "GREETING=hello from feature.env\n")
    template = template_with_environment({"GREETING": "${GREETING}"})

    result = server.deliver(
        app, ["deploy", "feature-42", "--image", f"web={harness.image()}"], template=template
    )

    assert result.outcome == "done", result.output
    assert get(server.slot_port("a", 1), "/env/GREETING") == "hello from feature.env"


def test_variables_from_the_apps_dev_env_dont_reach_its_feature_environments(
    harness: Harness,
) -> None:
    server = harness.server()
    app = server.apps["a"].name
    server.secrets(app, "dev", "GREETING=hello from dev.env\n")
    template = template_with_environment({"GREETING": "${GREETING}"})

    result = server.deliver(
        app, ["deploy", "feature-42", "--image", f"web={harness.image()}"], template=template
    )

    assert result.outcome == "done", result.output
    assert get(server.slot_port("a", 1), "/env/GREETING") == ""


def test_variables_from_the_apps_dev_env_reach_its_dev_environment_with_deadlifts_own(
    harness: Harness,
) -> None:
    server = harness.server()
    app = server.apps["a"].name
    server.secrets(app, "dev", "GREETING=hello from dev.env\n")
    server.secrets(app, "feature", "GREETING=hello from feature.env\n")
    template = template_with_environment(
        {"GREETING": "${GREETING}", "ENVIRONMENT": "${DEADLIFT_ENV}", "URL": "${DEADLIFT_URL}"}
    )

    result = server.deliver(
        app, ["deploy", "dev", "--image", f"web={harness.image()}"], template=template
    )

    assert result.outcome == "done", result.output
    assert get(server.dev_port("a"), "/env/GREETING") == "hello from dev.env"
    assert get(server.dev_port("a"), "/env/ENVIRONMENT") == "dev"
    assert get(server.dev_port("a"), "/env/URL") == f"https://{app}.tailtest.ts.net/"


def test_devs_deadlift_secret_stays_the_same_across_its_deploys(harness: Harness) -> None:
    server = harness.server()
    app = server.apps["a"].name
    template = template_with_environment({"SECRET": "${DEADLIFT_SECRET}"})

    def deploy(version: int) -> str:
        result = server.deliver(
            app,
            ["deploy", "dev", "--image", f"web={harness.image('healthy', version)}"],
            template=template,
        )
        assert result.outcome == "done", result.output
        return get(server.dev_port("a"), "/env/SECRET")

    first = deploy(1)
    second = deploy(2)

    assert matches(r"^[0-9a-f]{64}\Z", first)
    assert second == first


def test_one_apps_secrets_never_reach_another_apps_environments(harness: Harness) -> None:
    server = harness.server()
    a, b = server.apps["a"].name, server.apps["b"].name
    server.secrets(a, "feature", "GREETING=a's greeting\nA_ONLY=a's own secret\n")
    server.secrets(b, "feature", "GREETING=b's greeting\n")
    template = template_with_environment({"GREETING": "${GREETING}", "A_ONLY": "${A_ONLY}"})

    result = server.deliver(
        b, ["deploy", "feature-42", "--image", f"web={harness.image()}"], template=template
    )

    assert result.outcome == "done", result.output
    assert get(server.slot_port("b", 1), "/env/GREETING") == "b's greeting"
    assert get(server.slot_port("b", 1), "/env/A_ONLY") == ""


def test_a_secrets_file_cant_set_deadlifts_own_variables(harness: Harness) -> None:
    server = harness.server()
    app = server.apps["a"].name
    server.secrets(app, "feature", "DEADLIFT_ENV=dev\nDEADLIFT_APP=someone-else\n")
    template = template_with_environment(
        {"ENVIRONMENT": "${DEADLIFT_ENV}", "APP": "${DEADLIFT_APP}"}
    )

    result = server.deliver(
        app, ["deploy", "feature-42", "--image", f"web={harness.image()}"], template=template
    )

    assert result.outcome == "done", result.output
    assert get(server.slot_port("a", 1), "/env/ENVIRONMENT") == "feature-42"
    assert get(server.slot_port("a", 1), "/env/APP") == app


def test_a_deploy_fails_changing_nothing_when_the_apps_secrets_file_isnt_a_valid_env_file(
    harness: Harness,
) -> None:
    server = harness.server()
    app = server.apps["a"].name
    server.secrets(app, "feature", "GREETING=hello\nnot a variable\n")

    result = server.deliver(app, ["deploy", "feature-42", "--image", f"web={harness.image()}"])

    assert result.code == 1, result.output
    assert result.outcome == "failed"
    assert matches(r"^deadlift: .*feature\.env.*line 2", result.reason)
    assert matches(r"deadlift app secrets", result.reason)
    assert server.containers(app) == ""


def test_deadlift_env_deadlift_app_and_deadlift_url_hold_the_environment_the_app_and_its_url(
    harness: Harness,
) -> None:
    server = harness.server()
    app = server.apps["a"].name
    template = template_with_environment(
        {"ENVIRONMENT": "${DEADLIFT_ENV}", "APP": "${DEADLIFT_APP}", "URL": "${DEADLIFT_URL}"}
    )

    result = server.deliver(
        app, ["deploy", "feature-42", "--image", f"web={harness.image()}"], template=template
    )

    assert result.outcome == "done", result.output
    port = server.slot_port("a", 1)
    assert get(port, "/env/ENVIRONMENT") == "feature-42"
    assert get(port, "/env/APP") == app
    assert get(port, "/env/URL") == f"https://{app}.tailtest.ts.net:10001/"


def test_deadlift_secret_is_random_per_environment_and_the_same_across_redeploys(
    harness: Harness,
) -> None:
    server = harness.server()
    app = server.apps["a"].name
    template = template_with_environment({"SECRET": "${DEADLIFT_SECRET}"})

    def deploy(key: str) -> None:
        result = server.deliver(
            app,
            ["deploy", f"feature-{key}", "--image", f"web={harness.image()}"],
            template=template,
        )
        assert result.outcome == "done", result.output

    deploy("42")
    deploy("43")
    first = get(server.slot_port("a", 1), "/env/SECRET")
    second = get(server.slot_port("a", 2), "/env/SECRET")
    deploy("42")
    redeployed = get(server.slot_port("a", 1), "/env/SECRET")

    assert matches(r"^[0-9a-f]{64}\Z", first)
    assert matches(r"^[0-9a-f]{64}\Z", second)
    assert second != first
    assert redeployed == first


def test_deadlift_secret_is_new_after_the_environment_is_destroyed_and_deployed_again(
    harness: Harness,
) -> None:
    server = harness.server()
    app = server.apps["a"].name
    template = template_with_environment({"SECRET": "${DEADLIFT_SECRET}"})

    def deploy() -> str:
        result = server.deliver(
            app, ["deploy", "feature-42", "--image", f"web={harness.image()}"], template=template
        )
        assert result.outcome == "done", result.output
        return get(server.slot_port("a", 1), "/env/SECRET")

    before = deploy()
    destroyed = server.deliver(app, ["destroy", "feature-42"])
    assert destroyed.outcome == "done", destroyed.output
    after = deploy()

    assert matches(r"^[0-9a-f]{64}\Z", after)
    assert after != before
