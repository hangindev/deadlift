#!/usr/bin/env python3
"""build: builds an App's named images and pushes them to GHCR.

action.yml runs it as python3 -I "$GITHUB_ACTION_PATH/build.py"."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from deadlift.actions.build import main

sys.exit(main(sys.argv[1:]))
