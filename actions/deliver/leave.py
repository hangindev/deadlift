#!/usr/bin/env python3
"""leave: takes a CI job off the tailnet once the deliver action is done.

action.yml runs it as python3 -I "$GITHUB_ACTION_PATH/leave.py"."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from deadlift.actions.leave import main

sys.exit(main(sys.argv[1:]))
