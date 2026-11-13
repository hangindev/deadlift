#!/usr/bin/env bash
# Runs actionlint over this repo's workflows and deadlift's composite actions, each action written
# out as a workflow first (test/support/actions-as-workflows.ts). actionlint runs from its image,
# pinned, which also carries shellcheck for the `run:` scripts.
set -euo pipefail

image=rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667
repo=$(cd "$(dirname "$0")/.." && pwd)
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT

node "$repo/test/support/actions-as-workflows.ts" "$out"
# Readable by the image's own user.
chmod -R a+rX "$out"
files=(.github/workflows/*.yml)
for file in "$out"/*.yml; do files+=("/actions/${file##*/}"); done
docker run --rm -v "$repo:/repo:ro" -v "$out:/actions:ro" -w /repo "$image" "${files[@]}"
