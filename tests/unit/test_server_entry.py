"""What the entry command lets through of a delivery, judged from tar's listings of it."""

import pytest

from deadlift.core.outcome import Exit, Stop
from deadlift.server.entry import check_listing

FILE = "-rw-r--r-- runner/runner {size} 2026-11-14 01:21 {name}"


def listing(*members: tuple[str, int]) -> tuple[str, str]:
    rows = [FILE.format(size=size, name=name) for name, size in members]
    return "\n".join(rows), "\n".join(name for name, _ in members)


def refusal(listed: tuple[str, str]) -> str:
    with pytest.raises(Stop) as stopped:
        check_listing(*listed)
    assert stopped.value.code == Exit.REFUSED
    return str(stopped.value.why)


def test_a_template_and_a_token_get_through() -> None:
    check_listing(*listing(("compose.yaml", 300), ("./token", 40)))
    check_listing("", "")


def test_anything_else_is_refused() -> None:
    assert "it holds 'deploy.sh'" in refusal(listing(("compose.yaml", 1), ("deploy.sh", 1)))
    assert "it holds token twice" in refusal(listing(("token", 1), ("./token", 1)))
    assert "something other than plain files" in refusal(
        ("lrwxrwxrwx a/b 0 2026-11-14 01:21 compose.yaml -> /etc/passwd", "compose.yaml")
    )


def test_what_unpacks_past_the_limit_is_refused() -> None:
    assert "unpacks to over 1048576 bytes" in refusal(
        listing(("compose.yaml", 1048000), ("token", 1000))
    )


def test_an_owner_name_with_a_space_hides_the_size_as_in_the_bash() -> None:
    # A known fault kept for parity (followups.md): the size is read as tar -tv's third column.
    check_listing("-rw-r--r-- a b/c d 99999999 2026-11-14 01:21 compose.yaml", "compose.yaml")
