"""The example App's migration: brings the schema version kept in /data/schema up to its own
VERSION. It fails, changing nothing, when built with MIGRATE=fail, or when the data's schema is
newer than this version, as a real App's migration must (ADR 0002)."""

import os
import sys
from pathlib import Path

file = Path("/data/schema")
version = int(os.environ["EXAMPLE_VERSION"])
try:
    current = int(file.read_text())
except FileNotFoundError:
    # No data yet: a new Environment.
    current = 0

if os.environ.get("EXAMPLE_MIGRATE") == "fail":
    print("Example migration failing on purpose", file=sys.stderr)
    sys.exit(1)
if current > version:
    print(
        f"The data has schema {current}, and this version only knows up to {version}",
        file=sys.stderr,
    )
    sys.exit(1)
file.write_text(str(version))
print(f"Example migration: schema {current} to {version}")
