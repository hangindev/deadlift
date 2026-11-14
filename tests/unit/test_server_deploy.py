"""The deploy script's pure parts: reading --keep, image repositories, and the Server's disk."""

import pytest

from deadlift.server.deploy import keep_keys, repository_of
from deadlift.server.host import fullest_disk


@pytest.mark.parametrize(
    ("ref", "repository"),
    [
        ("ghcr.io/o/r/web:sha-1", "ghcr.io/o/r/web"),
        ("127.0.0.1:5000/o/web:sha-1", "127.0.0.1:5000/o/web"),
        ("127.0.0.1:5000/o/web", "127.0.0.1:5000/o/web"),
        ("alpine:3.20@sha256:abc", "alpine"),
        ("ghcr.io/o/web@sha256:abc", "ghcr.io/o/web"),
        ("alpine", "alpine"),
    ],
)
def test_an_image_reference_names_its_repository_without_tag_or_digest(
    ref: str, repository: str
) -> None:
    assert repository_of(ref) == repository


def test_keep_reads_keys_between_commas_as_bash_read_them() -> None:
    assert keep_keys("1,2") == ["1", "2"]
    assert keep_keys("1,2,") == ["1", "2"]
    assert keep_keys("1,,") == ["1", ""]
    assert keep_keys(",1") == ["", "1"]
    assert keep_keys("") == []
    assert keep_keys("1, 2") == ["1", " 2"]
    assert keep_keys("1\n2") == ["1"]


def test_the_disk_is_as_full_as_its_fullest_filesystem_in_df() -> None:
    df = (
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "/dev/sda1 100 45 55 45% /\n"
        "/dev/sdb1 100 81 19 81% /var/lib/docker\n"
    )
    assert fullest_disk(df) == "81"
    assert fullest_disk("Filesystem ...\n") == ""
