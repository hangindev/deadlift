"""deadlift: everything that ships, as one package with one subpackage per place it runs.

- ``deadlift.core``: what every other part shares (reason and output lines, exit codes, running
  children, the Server's lock). Standard library only.
- ``deadlift.server``: the Server's code, installed by bootstrap under
  /usr/local/lib/deadlift/python and run as root. Standard library and PyYAML (python3-yaml).
- ``deadlift.bootstrap``: turns a machine into a Server. Standard library only, since it runs
  before python3-yaml is installed.
- ``deadlift.cli``: the maintainer's ``deadlift`` command, run from a checkout. Standard library
  only.
- ``deadlift.actions``: the composite actions' scripts, run from ``$GITHUB_ACTION_PATH`` on CI
  runners. Standard library only.

Which part may import which, and what beyond the standard library, is checked by
tests/unit/test_layout.py. How each part is started without anything writable on ``sys.path`` is
ADR 0004's.
"""
