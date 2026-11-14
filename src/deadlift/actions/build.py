"""build: builds an App's named images and pushes them to GHCR with deadlift's tag convention, run
by the ``build`` action from the App's checkout.

Usage:
  build.py --repository <owner/name> --commit <sha> [--platform <platform>] [--source <url>]
           -- <image>...

  --repository  The App's repository. Its images are GHCR packages under it.
  --commit      The commit built, which names every image's tag.
  --platform    The one platform to build for, the Server's: linux/amd64 by default.
  --source      The repository's URL, labelled on each image so GHCR links the package to it.
  <image>       One image: its name, then any of context=<dir> (. by default),
                file=<Dockerfile> (<context>/Dockerfile by default) and target=<stage>,
                separated by spaces, as in `worker context=worker target=runtime`.

Each image is pushed as ghcr.io/<owner>/<name>/<image>:sha-<commit>, lowercased. That's what
``prune-registry`` keeps and deletes by, and it needs each version to be one single-platform
image with no attestations, which GHCR would list as untagged versions of their own.

Image names are lowercase letters and digits, joined by single ``_``, so that each reaches the
App's template as ${DEADLIFT_IMAGE_<NAME>}.

Environment:
  REGISTRY_USERNAME, REGISTRY_TOKEN  The login to GHCR, with packages: write. It's kept in a
                                     Docker config of this run's own, removed afterwards.
  GITHUB_OUTPUT        When set, gets ``images``: one <name>=<ref> per line, as ``deliver`` takes.
  GITHUB_STEP_SUMMARY  When set, gets the references pushed.

Exit codes: 0 every image pushed, 1 a build or push failed, 2 refused (nothing built).
"""

import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NoReturn

from deadlift.actions import job
from deadlift.core.names import image_name_problem, is_repository
from deadlift.core.outcome import Exit, Stop, fail, reason, refuse, run_main
from deadlift.core.process import Env, Stream, run

USAGE = (
    "build.py --repository <owner/name> --commit <sha> [--platform <platform>] [--source <url>]"
    " -- <image>..."
)

_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_PLATFORM = re.compile(r"[a-z0-9]+/[a-z0-9]+(/[a-z0-9]+)?")
_SETTINGS = ("context", "file", "target")


@dataclass(frozen=True)
class Image:
    """One image to build: its name, and where and how from."""

    name: str
    context: str
    file: str
    target: str


def parse_images(specs: Sequence[str]) -> list[Image]:
    """The images ``specs`` name, one per spec, in order; blank specs name none. Refuses (Stop)
    a bad name or setting, a name given twice, or no image at all."""
    images: list[Image] = []
    for spec in specs:
        name, *settings = job.words(spec) or [""]
        if not name:
            continue
        problem = image_name_problem(name)
        if problem is not None:
            refuse(problem)
        if any(image.name == name for image in images):
            refuse(f"the image {name} is given twice")
        given = {"context": ".", "file": "", "target": ""}
        for setting in settings:
            key, _, value = setting.partition("=")
            if key not in _SETTINGS or not value:
                refuse(
                    f"'{setting}' isn't a setting of the image {name}: context=, file= or target="
                )
            given[key] = value
        context = given["context"]
        images.append(
            Image(name, context, given["file"] or f"{context}/Dockerfile", given["target"])
        )
    if not images:
        refuse("no image was named")
    return images


def _usage(why: str) -> NoReturn:
    job.usage(f"{why}, so nothing was built.", USAGE)


def _build(argv: Sequence[str]) -> Exit:
    options = {"repository": "", "commit": "", "platform": "linux/amd64", "source": ""}
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg == "--":
            break
        if arg.startswith("--") and arg[2:] in options:
            if not args or not args[0]:
                _usage(f"{arg} needs a value")
            options[arg[2:]] = args.pop(0)
            continue
        _usage(f"unknown argument '{arg}'")
    repository, commit, platform = options["repository"], options["commit"], options["platform"]
    if not is_repository(repository):
        _usage("--repository needs <owner>/<name>")
    if not _COMMIT.fullmatch(commit):
        _usage(f"--commit needs a full commit SHA, not '{commit}'")
    if not _PLATFORM.fullmatch(platform):
        _usage(f"--platform needs one platform, such as linux/amd64, not '{platform}'")
    username = os.environ.get("REGISTRY_USERNAME", "")
    token = os.environ.get("REGISTRY_TOKEN", "")
    if not username or not token:
        _usage("REGISTRY_USERNAME and REGISTRY_TOKEN must be set")
    try:
        images = parse_images(args)
    except Stop as stop:
        _usage(stop.why or "")

    with tempfile.TemporaryDirectory() as docker_config:
        env = job.job_env({"DOCKER_CONFIG": docker_config})
        login = run(
            ["docker", "login", "ghcr.io", "--username", username, "--password-stdin"],
            env=env,
            input=f"{token}\n",
            stdout=Stream.DEVNULL,
        )
        if not login.ok:
            fail(f"couldn't log in to ghcr.io as {username}, so nothing was built.")
        pushed = _push(
            images,
            repository=repository,
            commit=commit,
            platform=platform,
            source=options["source"],
            env=env,
        )

    job.append("GITHUB_OUTPUT", ["images<<DEADLIFT_IMAGES", *pushed.values(), "DEADLIFT_IMAGES"])
    refs = [ref.partition("=")[2] for ref in pushed.values()]
    plural = "" if len(refs) == 1 else "s"
    job.append(
        "GITHUB_STEP_SUMMARY",
        [f"### deadlift build: pushed {len(refs)} image{plural}", "", *(f"- `{r}`" for r in refs)],
    )
    reason(f"pushed {' '.join(refs)}")
    return Exit.DONE


def _push(
    images: list[Image], *, repository: str, commit: str, platform: str, source: str, env: Env
) -> dict[str, str]:
    """Builds and pushes each image; ``<name>=<ref>`` of each pushed, by name."""
    pushed: dict[str, str] = {}
    for image in images:
        ref = f"ghcr.io/{repository.lower()}/{image.name}:sha-{commit}"
        args = ["--platform", platform, "--provenance=false", "--sbom=false"]
        args += ["--file", image.file, "--tag", ref, "--push"]
        if source:
            args += ["--label", f"org.opencontainers.image.source={source}"]
        args += ["--label", f"org.opencontainers.image.revision={commit}"]
        if image.target:
            args += ["--target", image.target]
        job.say(f"Building {image.name} as {ref}")
        built = run(
            ["docker", "buildx", "build", *args, image.context], env=env, stdout=Stream.INHERIT
        )
        if not built.ok:
            before = " ".join(ref.partition("=")[2] for ref in pushed.values())
            fail(
                f"couldn't build and push the image {image.name}."
                + (f" Pushed before it: {before}." if before else "")
            )
        pushed[image.name] = f"{image.name}={ref}"
    return pushed


def main(argv: Sequence[str]) -> int:
    return run_main(lambda: _build(argv))
