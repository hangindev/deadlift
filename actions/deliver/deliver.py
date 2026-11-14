#!/usr/bin/env python3
"""deliver: the client half of a delivery, which the delivery tests also drive.

action.yml runs it as python3 -I "$GITHUB_ACTION_PATH/deliver.py"."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from deadlift.actions.deliver import main

sys.exit(main(sys.argv[1:]))
