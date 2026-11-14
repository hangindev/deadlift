import pytest

from deadlift.actions.prune_registry import Version, keep_list, parse_versions, to_delete

DIGEST = "sha256:" + "a" * 64


def test_a_keep_list_is_split_on_commas_and_whitespace() -> None:
    assert keep_list(" sha-c1,sha-c2\n\tnightly ,, sha256:ab ") == [
        "sha-c1",
        "sha-c2",
        "nightly",
        "sha256:ab",
    ]


@pytest.mark.parametrize("text", ["", " ", " , \n", ",,"])
def test_a_keep_list_of_only_separators_is_empty(text: str) -> None:
    assert keep_list(text) == []


def test_versions_are_read_from_each_line_gh_prints() -> None:
    stdout = (
        '{"id":1,"name":"sha256:1","metadata":{"container":{"tags":["sha-c1"]}}}\n'
        '{"id":2,"name":"sha256:2","metadata":{"package_type":"container",'
        '"container":{"tags":[]}},"created_at":"2026-11-13T00:00:00Z"}\n'
    )
    assert parse_versions(stdout) == [
        Version(1, "sha256:1", ("sha-c1",)),
        Version(2, "sha256:2", ()),
    ]


def test_versions_may_span_lines_and_pages() -> None:
    stdout = '{\n  "id": 1,\n  "name": "d1",\n  "metadata": {"container": {"tags": ["a"]}}\n}'
    assert parse_versions(stdout + stdout) == [Version(1, "d1", ("a",))] * 2


def test_no_versions_is_an_empty_list() -> None:
    assert parse_versions("") == []


@pytest.mark.parametrize(
    "stdout",
    [
        "not json",
        '{"id":1,"name":"d1"}',
        '{"id":"1","name":"d1","metadata":{"container":{"tags":[]}}}',
        '{"id":1,"name":"d1","metadata":{"container":{"tags":[1]}}}',
        '[{"id":1}]',
    ],
)
def test_anything_else_isnt_a_list_of_versions(stdout: str) -> None:
    with pytest.raises(ValueError):
        parse_versions(stdout)


def test_a_version_is_kept_by_any_of_its_tags_or_its_digest_and_the_rest_are_deleted() -> None:
    by_tag = Version(1, "sha256:1", ("sha-c2", "nightly"))
    by_digest = Version(2, DIGEST, ("sha-c1",))
    untagged = Version(3, "sha256:3", ())
    tagged = Version(4, "sha256:4", ("sha-c0", "old"))

    assert to_delete([by_tag, by_digest, untagged, tagged], ["nightly", DIGEST], "web") == [
        (3, "web@sha256:3"),
        (4, "web:sha-c0,old"),
    ]


def test_keeping_nothing_deletes_every_version() -> None:
    assert to_delete([Version(1, "d1", ("a",)), Version(2, "d2", ())], [], "worker") == [
        (1, "worker:a"),
        (2, "worker@d2"),
    ]
