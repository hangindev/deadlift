"""The setup-deadlift skill's merge_policy.py, run as the skill runs it: the maintainer's tailnet
policy and the entries a wizard printed in, the merged policy out, and nothing else changed."""

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "setup-deadlift" / "scripts" / "merge_policy.py"


@dataclass(frozen=True)
class Merged:
    status: int
    stdout: str
    stderr: str


def merge(tmp_path: Path, policy: str, *entries: str) -> Merged:
    policy_file = tmp_path / "policy.hujson"
    policy_file.write_text(policy, encoding="utf-8")
    entries_files: list[str] = []
    for n, text in enumerate(entries):
        entries_file = tmp_path / f"entries-{n}.hujson"
        entries_file.write_text(text, encoding="utf-8")
        entries_files.append(str(entries_file))
    done = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(policy_file), *entries_files],
        capture_output=True,
        text=True,
        check=False,
    )
    return Merged(done.returncode, done.stdout, done.stderr)


def test_adds_a_section_the_policy_lacks_at_the_end_keeping_its_comments(tmp_path: Path) -> None:
    policy = """// Our tailnet.
{
	// Who owns which tag.
	"tagOwners": {
		"tag:ops": ["autogroup:admin"], // the ops boxes
	},
}
"""
    entries = """{
  "ssh": [
    {
      "action": "accept",
      "src": ["tag:deadlift-ci-shop"],
      "dst": ["tag:deadlift-server"],
      "users": ["deadlift-shop"]
    }
  ]
}
"""

    merged = merge(tmp_path, policy, entries)

    assert merged.status == 0, merged.stderr
    assert merged.stdout == """// Our tailnet.
{
	// Who owns which tag.
	"tagOwners": {
		"tag:ops": ["autogroup:admin"], // the ops boxes
	},

	"ssh": [
		{
			"action": "accept",
			"src": ["tag:deadlift-ci-shop"],
			"dst": ["tag:deadlift-server"],
			"users": ["deadlift-shop"]
		}
	],
}
"""


# A policy laid out as Tailscale's default one is, with the App's entries as `deadlift app add`
# prints them.
DEFAULT_LIKE = """// Example/default ACLs for unrestricted connections.
{
	// Define the tags which can be applied to devices and by which users.
	// "tagOwners": {
	//   "tag:example": ["autogroup:admin"],
	// },

	// Define grants that govern access for users, groups, autogroups, tags,
	// Tailscale IP addresses, and subnet ranges.
	"grants": [
		// Allow all connections.
		// Comment this section out if you want to define specific restrictions.
		{"src": ["*"], "dst": ["*"], "ip": ["*"]},
	],

	// Define users and devices that can use Tailscale SSH.
	"ssh": [
		// Allow all users to SSH into their own devices in check mode.
		{
			"action": "check",
			"src":    ["autogroup:member"],
			"dst":    ["autogroup:self"],
			"users":  ["autogroup:nonroot", "root"],
		},
	],
}
"""

APP_ENTRIES = """{
  "tagOwners": {
    "tag:deadlift-ci-shop": [
      "autogroup:admin"
    ]
  },
  "grants": [
    {
      "src": [
        "tag:deadlift-ci-shop"
      ],
      "dst": [
        "tag:deadlift-server"
      ],
      "ip": [
        "tcp:22"
      ]
    }
  ],
  "ssh": [
    {
      "action": "accept",
      "src": [
        "tag:deadlift-ci-shop"
      ],
      "dst": [
        "tag:deadlift-server"
      ],
      "users": [
        "deadlift-shop"
      ]
    }
  ]
}
"""


def test_appends_each_entry_after_the_last_of_its_section(tmp_path: Path) -> None:
    merged = merge(tmp_path, DEFAULT_LIKE, APP_ENTRIES)

    assert merged.status == 0, merged.stderr
    assert merged.stdout == """// Example/default ACLs for unrestricted connections.
{
	// Define the tags which can be applied to devices and by which users.
	// "tagOwners": {
	//   "tag:example": ["autogroup:admin"],
	// },

	// Define grants that govern access for users, groups, autogroups, tags,
	// Tailscale IP addresses, and subnet ranges.
	"grants": [
		// Allow all connections.
		// Comment this section out if you want to define specific restrictions.
		{"src": ["*"], "dst": ["*"], "ip": ["*"]},
		{
			"src": [
				"tag:deadlift-ci-shop"
			],
			"dst": [
				"tag:deadlift-server"
			],
			"ip": [
				"tcp:22"
			]
		},
	],

	// Define users and devices that can use Tailscale SSH.
	"ssh": [
		// Allow all users to SSH into their own devices in check mode.
		{
			"action": "check",
			"src":    ["autogroup:member"],
			"dst":    ["autogroup:self"],
			"users":  ["autogroup:nonroot", "root"],
		},
		{
			"action": "accept",
			"src": [
				"tag:deadlift-ci-shop"
			],
			"dst": [
				"tag:deadlift-server"
			],
			"users": [
				"deadlift-shop"
			]
		},
	],

	"tagOwners": {
		"tag:deadlift-ci-shop": [
			"autogroup:admin"
		]
	},
}
"""
    assert "deadlift: added tagOwners" in merged.stderr


def test_merging_again_changes_nothing(tmp_path: Path) -> None:
    once = merge(tmp_path, DEFAULT_LIKE, APP_ENTRIES).stdout

    again = merge(tmp_path, once, APP_ENTRIES)

    assert again.status == 0, again.stderr
    assert again.stdout == once
    assert "nothing was added" in again.stderr
