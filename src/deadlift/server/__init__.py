"""The Server's code: the entry command, the deploy script, the template check, the Server half of
``deadlift app`` and ``server status``, and the firewall. Bootstrap installs this package, with
``deadlift.core``, root-owned under /usr/local/lib/deadlift/python, and every entry point runs
as ``python3 -I`` through a launcher that loads it from there (ADR 0004). Standard library and
PyYAML only.
"""
