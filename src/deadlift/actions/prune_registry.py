"""Deletes an App's image versions from GHCR, except the ones it's told to keep. It knows nothing
about pull requests: what to keep is the caller's choice.

Usage: prune-registry.py [--dry-run] (--keep <versions> | --keep-none) <image>...
  <versions>   tags (such as sha-<commit>) or digests, separated by commas or whitespace. A
               version is kept when any of its tags, or its digest, is listed.
  --keep-none  delete every version. An empty keep list is refused without it, and so is one
               that names none of an image's versions, since either would delete them all.
  --dry-run    say what would be deleted, and delete nothing.
An image with no versions in GHCR, such as a new App's before its first build pushed it, has
nothing to delete, and the closing reason line names it.
Needs GH_TOKEN with packages: write (and each package giving the repository the Admin role), and
GITHUB_REPOSITORY. Exits 0 done, 1 failed, 2 refused (nothing deleted).
"""

import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from deadlift.actions import job
from deadlift.core.names import image_name_problem, is_repository
from deadlift.core.outcome import Exit, fail, reason, refuse, run_main
from deadlift.core.process import Env, Stream, run

USAGE = "usage: prune-registry.py [--dry-run] (--keep <versions> | --keep-none) <image>..."

_SEPARATORS = re.compile(r"[,\s]+", re.ASCII)


@dataclass(frozen=True)
class Version:
    """One version of an image in GHCR: its id, its digest (GHCR's name for it) and its tags."""

    id: int
    name: str
    tags: tuple[str, ...]


def keep_list(text: str) -> list[str]:
    """The versions to keep, from tags or digests separated by commas or whitespace."""
    return [word for word in _SEPARATORS.split(text) if word]


def _get(value: object, key: str) -> object:
    """``value[key]`` when ``value`` is a JSON object holding it, else None."""
    return cast(dict[str, object], value).get(key) if isinstance(value, dict) else None


def _version(value: object) -> Version:
    id_, name = _get(value, "id"), _get(value, "name")
    tags = _get(_get(_get(value, "metadata"), "container"), "tags")
    tag_list = cast(list[object], tags) if isinstance(tags, list) else None
    if (
        isinstance(id_, int)
        and not isinstance(id_, bool)
        and isinstance(name, str)
        and tag_list is not None
    ):
        strings = tuple(tag for tag in tag_list if isinstance(tag, str))
        if len(strings) == len(tag_list):
            return Version(id_, name, strings)
    raise ValueError(f"{value!r} isn't a GHCR package version")


def parse_versions(stdout: str) -> list[Version]:
    """The versions ``gh api --paginate <versions> --jq '.[]'`` printed: JSON values, one after
    another. ValueError when they aren't versions."""
    return [_version(value) for value in job.json_values(stdout)]


def to_delete(
    versions: Sequence[Version], keep: Sequence[str], image: str
) -> list[tuple[int, str]]:
    """The id of each of ``image``'s versions that ``keep`` doesn't name, by a tag or its digest,
    and what it is: ``<image>:<tags>``, or ``<image>@<digest>`` when it has no tag."""
    return [
        (
            version.id,
            f"{image}:{','.join(version.tags)}" if version.tags else f"{image}@{version.name}",
        )
        for version in versions
        if not _kept(version, keep)
    ]


def _kept(version: Version, keep: Sequence[str]) -> bool:
    return version.name in keep or any(tag in keep for tag in version.tags)


def _gh(args: Sequence[str], env: Env, *, capture: bool = True) -> str | None:
    """``gh api <args>``'s output, or None when it failed."""
    done = run(["gh", "api", *args], env=env, stdout=Stream.CAPTURE if capture else Stream.DEVNULL)
    return done.stdout if done.ok else None


class _NotFound(Exception):
    """GitHub has no such package, or none the token can see: it answers 404 for both."""


def _list_versions(versions_url: str, env: Env) -> list[Version] | None:
    """An image's versions, or None when they couldn't be listed. Raises :class:`_NotFound`
    when GitHub has no such package."""
    done = run(
        ["gh", "api", "--paginate", f"{versions_url}?per_page=100", "--jq", ".[]"],
        env=env,
        stdout=Stream.CAPTURE,
        stderr=Stream.CAPTURE,
    )
    if not done.ok:
        if "(HTTP 404)" in done.stderr:
            raise _NotFound
        sys.stderr.write(done.stderr)
        sys.stderr.flush()
        return None
    try:
        return parse_versions(done.stdout)
    except ValueError:
        return None


def _prune(argv: Sequence[str]) -> Exit:
    keep_text = ""
    keep_given = keep_none = dry_run = False
    args = list(argv)
    while args:
        arg = args[0]
        if arg == "--keep":
            if len(args) < 2:
                refuse(USAGE)
            keep_text += f" {args[1]}"
            keep_given = True
            del args[:2]
        elif arg == "--keep-none":
            keep_none = True
            del args[0]
        elif arg == "--dry-run":
            dry_run = True
            del args[0]
        elif arg == "--":
            del args[0]
            break
        elif arg.startswith("-"):
            refuse(USAGE)
        else:
            break
    images = args

    keep = keep_list(keep_text)
    if keep_none:
        if keep_given:
            refuse("pass either --keep or --keep-none, not both.")
    elif not keep:
        refuse(
            "refused: no versions to keep were given, which would delete every version, so nothing"
            " was deleted. Pass --keep-none (keep-none: true) to mean that."
        )

    if not images:
        refuse(USAGE)
    for image in images:
        problem = image_name_problem(image)
        if problem is not None:
            refuse(f"{problem}.")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not is_repository(repository):
        refuse(f"GITHUB_REPOSITORY must be <owner>/<name>, not '{repository}'.")
    # GHCR names an image ghcr.io/<owner>/<repository>/<image>, all lowercase, and its package
    # <repository>/<image>.
    repository = repository.lower()
    owner, _, name = repository.partition("/")
    env = job.job_env()
    # GHCR's API puts a user's packages under users/ and an organisation's under orgs/.
    owner_type = _gh([f"repos/{repository}", "--jq", ".owner.type"], env)
    if owner_type is None:
        fail(f"couldn't look up {repository} to find its packages, so nothing was deleted.")
    owner_path = f"orgs/{owner}" if owner_type.rstrip("\n") == "Organization" else f"users/{owner}"

    # Every image's versions are listed and checked before any is deleted. An image with none,
    # such as a new App's before its first build, has nothing to delete.
    doomed: list[tuple[str, int, str]] = []
    total = 0
    missing: list[str] = []
    for image in images:
        versions_url = f"{owner_path}/packages/container/{name}%2F{image}/versions"
        try:
            versions = _list_versions(versions_url, env)
        except _NotFound:
            missing.append(image)
            continue
        if versions is None:
            fail(
                f"couldn't list {image}'s versions in GHCR (ghcr.io/{owner}/{name}/{image}), so"
                f" nothing was deleted. The package must exist and give {repository} the Admin"
                " role (its settings, Manage Actions access), and the token needs packages: write."
            )
        total += len(versions)
        if versions and not keep_none and not any(_kept(version, keep) for version in versions):
            refuse(
                f"refused: none of {image}'s {len(versions)} versions is one to keep, which would"
                " delete them all, so nothing was deleted. Keep names versions by tag (such as"
                " sha-<commit>) or digest."
            )
        doomed += [(versions_url, id_, what) for id_, what in to_delete(versions, keep, image)]

    deleted = 0
    for versions_url, id_, what in doomed:
        if dry_run:
            job.say(f"Would delete {what}")
            deleted += 1
            continue
        if _gh(["--method", "DELETE", f"{versions_url}/{id_}"], env, capture=False) is None:
            fail(
                f"couldn't delete {what}. {deleted} versions were deleted before it, and the rest"
                f" are still there. Deleting needs the package to give {repository} the Admin"
                " role (its settings, Manage Actions access)."
            )
        job.say(f"Deleted {what}")
        deleted += 1
    listing = ", ".join(images)
    gone = ""
    if missing:
        gone = (
            f" {_sentence(missing)} {'is' if len(missing) == 1 else 'are'}n't in GHCR"
            f" (ghcr.io/{owner}/{name}/<image>) yet, or the token can't see"
            f" {'it' if len(missing) == 1 else 'them'}, so there was nothing of"
            f" {'it' if len(missing) == 1 else 'them'} to delete."
        )
    if dry_run:
        reason(f"would delete {deleted} of {total} versions of {listing} (a dry run).{gone}")
    else:
        reason(f"deleted {deleted} of {total} versions of {listing}.{gone}")
    return Exit.DONE


def _sentence(names: Sequence[str]) -> str:
    """Joins ``names`` as in a sentence: "a", "a and b", "a, b and c"."""
    if len(names) < 2:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def main(argv: Sequence[str]) -> int:
    return run_main(lambda: _prune(argv))
