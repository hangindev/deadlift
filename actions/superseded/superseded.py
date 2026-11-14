#!/usr/bin/env python3
"""superseded: says whether the tracked branch has moved past a commit.

action.yml runs it as python3 -I "$GITHUB_ACTION_PATH/superseded.py"."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from deadlift.actions.superseded import main

sys.exit(main(sys.argv[1:]))
