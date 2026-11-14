#!/usr/bin/env bash
# Runs actionlint over this repo's workflows, deadlift's composite actions, each written out as a
# workflow first (tests/support/actions_as_workflows.py, run through uv for PyYAML), and the
# starter workflows as `deadlift init` writes them into an App's repository. actionlint runs from
# its image, pinned, which also carries shellcheck for the `run:` scripts.
set -euo pipefail

image=rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667
repo=$(cd "$(dirname "$0")/.." && pwd)
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT

mkdir "$out/actions" "$out/app"
(cd "$repo" && uv run --locked python -m tests.support.actions_as_workflows "$out/actions")
# An App with two images, so that the lists of them are checked too.
git init --quiet "$out/app"
(cd "$out/app" && "$repo/bin/deadlift" init example --server deadlift-1 --image web --image worker >/dev/null)
# Readable by the image's own user.
chmod -R a+rX "$out"
files=(.github/workflows/*.yml)
for file in "$out"/actions/*.yml; do files+=("/actions/${file##*/}"); done
for file in "$out"/app/.github/workflows/*.yml; do files+=("/app/${file##*/}"); done
docker run --rm -v "$repo:/repo:ro" -v "$out/actions:/actions:ro" -v "$out/app/.github/workflows:/app:ro" \
  -w /repo "$image" "${files[@]}"
