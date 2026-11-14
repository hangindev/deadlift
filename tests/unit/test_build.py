import pytest

from deadlift.actions.build import Image, parse_images
from deadlift.core.outcome import Exit, Stop


def test_an_image_is_built_from_the_checkout_by_default() -> None:
    assert parse_images(["web"]) == [Image("web", context=".", file="./Dockerfile", target="")]


def test_an_image_takes_a_context_file_and_target_in_any_order() -> None:
    assert parse_images(["worker  target=runtime\tcontext=worker", "api file=api.Dockerfile"]) == [
        Image("worker", context="worker", file="worker/Dockerfile", target="runtime"),
        Image("api", context=".", file="api.Dockerfile", target=""),
    ]


def test_a_setting_keeps_everything_after_its_first_equals_sign() -> None:
    assert parse_images(["web target=a=b"])[0].target == "a=b"


def test_blank_lines_name_no_image() -> None:
    assert [image.name for image in parse_images(["", "web", "  ", "worker"])] == ["web", "worker"]


@pytest.mark.parametrize(
    ("specs", "why"),
    [
        ([], "no image was named"),
        (["", " "], "no image was named"),
        (
            ["Web"],
            "'Web' isn't an image name: lowercase letters and digits, starting with a letter,"
            " joined by single _",
        ),
        (["web__api"], "'web__api' isn't an image name"),
        (["1web"], "'1web' isn't an image name"),
        (["web", "web context=x"], "the image web is given twice"),
        (
            ["web context="],
            "'context=' isn't a setting of the image web: context=, file= or target=",
        ),
        (["web platform=x"], "'platform=x' isn't a setting of the image web"),
    ],
)
def test_a_bad_image_list_is_refused(specs: list[str], why: str) -> None:
    with pytest.raises(Stop) as stopped:
        parse_images(specs)
    assert stopped.value.code is Exit.REFUSED
    assert stopped.value.why is not None
    assert stopped.value.why.startswith(why)
