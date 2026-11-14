#!/usr/bin/env python3
"""prune-registry: deletes an App's image versions from GHCR but the ones to keep.

action.yml runs it as python3 -I "$GITHUB_ACTION_PATH/prune-registry.py"."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from deadlift.actions.prune_registry import main

sys.exit(main(sys.argv[1:]))
