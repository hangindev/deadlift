"""Bootstrap's decisions that need no machine: reading /etc/os-release, Docker's config and the
Server-wide settings."""

import pytest

from deadlift.bootstrap import bootstrap

NOBLE = """\
PRETTY_NAME="Ubuntu 24.04.1 LTS"
NAME="Ubuntu"
VERSION_ID="24.04"
VERSION_CODENAME=noble
ID=ubuntu
ID_LIKE=debian
UBUNTU_CODENAME=noble
"""


def test_reads_os_release() -> None:
    release = bootstrap.os_release(NOBLE)

    assert release["ID"] == "ubuntu"
    assert release["PRETTY_NAME"] == "Ubuntu 24.04.1 LTS"


def test_takes_ubuntus_codename_before_the_versions() -> None:
    assert bootstrap.codename({"UBUNTU_CODENAME": "noble", "VERSION_CODENAME": "x"}) == "noble"
    assert bootstrap.codename({"VERSION_CODENAME": "jammy"}) == "jammy"
    assert bootstrap.codename({}) == ""


def test_turns_on_userns_remap_keeping_the_rest_of_dockers_config() -> None:
    config = '{"data-root": "/mnt/docker", "log-opts": {"max-size": "10m"}}'

    assert bootstrap.remapped_docker_config(config) == (
        "{\n"
        '  "data-root": "/mnt/docker",\n'
        '  "log-opts": {\n'
        '    "max-size": "10m"\n'
        "  },\n"
        '  "userns-remap": "default"\n'
        "}"
    )


def test_writes_a_new_docker_config_with_only_userns_remap() -> None:
    assert bootstrap.remapped_docker_config(None) == '{\n  "userns-remap": "default"\n}'


def test_replaces_an_empty_userns_remap() -> None:
    assert bootstrap.remapped_docker_config('{"userns-remap": "", "debug": true}') == (
        '{\n  "userns-remap": "default",\n  "debug": true\n}'
    )


def test_leaves_a_docker_config_that_remaps_already() -> None:
    assert bootstrap.remapped_docker_config('{"userns-remap": "dockremap"}') is None


@pytest.mark.parametrize("config", ["{", "[]", '"remap"'])
def test_refuses_a_docker_config_that_isnt_a_json_object(config: str) -> None:
    with pytest.raises(ValueError):
        bootstrap.remapped_docker_config(config)


def test_adds_only_the_settings_missing_from_the_file() -> None:
    settings = "# comment\nfeature_deploy_disk_limit=70\n"

    assert bootstrap.missing_settings(settings) == ["watchdog_disk_limit=90"]
    assert bootstrap.missing_settings("") == [
        "feature_deploy_disk_limit=80",
        "watchdog_disk_limit=90",
    ]
