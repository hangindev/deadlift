"""The example App's Compose healthcheck: exit 0 when `/health` answers 200."""

import sys
import urllib.request

try:
    with urllib.request.urlopen("http://127.0.0.1:3000/health", timeout=2) as response:
        sys.exit(0 if response.status == 200 else 1)
except OSError:
    sys.exit(1)
