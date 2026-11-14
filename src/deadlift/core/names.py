"""The names deadlift checks in more than one place, each with one rule: an App's, an image's and
a GitHub repository's. The CLI, the actions and the Server all check them, and a name one of them
accepts must be one the others accept too.
"""

import re

_APP = re.compile(r"[a-z]([a-z0-9-]{0,21}[a-z0-9])?")
_APP_FEATURE_PART = re.compile(r"-feature(-|$)")
_IMAGE = re.compile(r"[a-z][a-z0-9]*(_[a-z0-9]+)*")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def app_name_problem(name: str) -> str | None:
    """Why ``name`` can't be an App's name, or None when it can.

    deadlift uses it in a user name, a Tailscale Service name, a tag and the Compose projects
    deadlift-<app>-<environment>: up to 23 of a-z, 0-9 and -, starting with a letter and not
    ending with -. A name with a ``-feature`` part could make one App's project the same as
    another's (deadlift-x-feature-y-dev is App x's feature-y-dev and App x-feature-y's dev), so
    it's refused too.
    """
    if not _APP.fullmatch(name):
        return (
            f"'{name}' isn't an App name: up to 23 of a-z, 0-9 and -, starting with a letter and"
            " not ending with -"
        )
    if _APP_FEATURE_PART.search(name):
        return (
            f"'{name}' isn't an App name: a part named feature after a - could clash with another"
            " App's Feature Environments"
        )
    return None


def is_app_name(name: str) -> bool:
    """Whether ``name`` can be an App's name (see :func:`app_name_problem`)."""
    return app_name_problem(name) is None


def is_image_name(name: str) -> bool:
    """Whether ``name`` can name one of an App's images: lowercase letters and digits, starting
    with a letter, joined by single ``_``, so it reaches the template as
    ``${DEADLIFT_IMAGE_<NAME>}`` and GHCR as part of ghcr.io/<owner>/<repo>/<name>."""
    return _IMAGE.fullmatch(name) is not None


def image_name_problem(name: str) -> str | None:
    """Why ``name`` can't name an image (see :func:`is_image_name`), or None when it can."""
    if is_image_name(name):
        return None
    return (
        f"'{name}' isn't an image name: lowercase letters and digits, starting with a letter,"
        " joined by single _"
    )


def is_repository(name: str) -> bool:
    """Whether ``name`` is a GitHub repository as ``<owner>/<name>``."""
    return _REPOSITORY.fullmatch(name) is not None
