"""``deadlift init``'s rendering of a starter, and its header, apart from git and the files."""

import hashlib

from deadlift.cli.init import after_header, render, workflow, written_hash

STARTER = """\
name: @app@-dev
env:
  DEADLIFT_SERVER: @server@
on: {push: {branches: [@branch@]}}
images: @image-names@
build:
  - |
    @images@
end

"""


def test_a_starter_gets_the_apps_values_and_one_line_per_image() -> None:
    rendered = render(
        STARTER, app="shop", server="deadlift-1", branch="main", images=["web", "api"]
    )

    assert rendered == (
        "name: shop-dev\n"
        "env:\n"
        "  DEADLIFT_SERVER: deadlift-1\n"
        "on: {push: {branches: [main]}}\n"
        "images: web api\n"
        "build:\n"
        "  - |\n"
        "    web\n"
        "    api\n"
        "end"
    )


def test_a_starter_without_a_last_line_break_is_rendered_whole() -> None:
    assert render("a: @app@", app="x", server="s", branch="b", images=["web"]) == "a: x"


def test_a_workflow_starts_with_a_header_holding_the_hash_of_the_rest() -> None:
    data = workflow("name: x\non: push")

    lines = data.split(b"\n")
    assert lines[0] == (
        b"# Written by `deadlift init`, and the App's own: once this file changes, deadlift init"
    )
    digest = hashlib.sha256(b"name: x\non: push\n").hexdigest()
    assert lines[1] == f"# leaves it alone. Starter sha256:{digest}".encode()
    assert lines[2] == b"#"
    assert after_header(data) == b"name: x\non: push\n"
    assert written_hash(data) == digest.encode()


def test_a_file_deadlift_init_didnt_write_has_no_hash() -> None:
    assert written_hash(b"name: mine\n") is None
    assert written_hash(b"#\n# Starter sha256:abc\n") is None
    assert written_hash(b"") is None


def test_after_the_header_is_nothing_for_a_short_file() -> None:
    assert after_header(b"a\nb\nc") == b""
    assert after_header(b"a\nb\nc\n") == b""
    assert after_header(b"a\nb\nc\nd") == b"d"
