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
    assert (
        merged.stdout
        == """// Our tailnet.
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
    )


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
    assert (
        merged.stdout
        == """// Example/default ACLs for unrestricted connections.
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
    )
    assert "deadlift: added tagOwners" in merged.stderr


def test_merging_again_changes_nothing(tmp_path: Path) -> None:
    once = merge(tmp_path, DEFAULT_LIKE, APP_ENTRIES).stdout

    again = merge(tmp_path, once, APP_ENTRIES)

    assert again.status == 0, again.stderr
    assert again.stdout == once
    assert "nothing was added" in again.stderr


def test_merges_into_sections_the_policy_has_level_by_level(tmp_path: Path) -> None:
    policy = """{
  "tagOwners": {
    "tag:deadlift-server": ["group:ops"],
    "tag:deadlift-svc": ["autogroup:admin"]
  },
  "autoApprovers": {
    "routes": {"10.0.0.0/8": ["tag:router"]}
  }
}
"""
    entries = """{
	"tagOwners": {
		// Servers, joined by an admin in the provision wizard.
		"tag:deadlift-server": ["autogroup:admin"],
		"tag:deadlift-svc": ["autogroup:admin"],
	},
	"autoApprovers": {
		// Any Server may serve any App's Service, with no approval by hand.
		"services": {
			"tag:deadlift-svc": ["tag:deadlift-server"],
		},
	},
}
"""

    merged = merge(tmp_path, policy, entries)

    assert merged.status == 0, merged.stderr
    assert (
        merged.stdout
        == """{
  "tagOwners": {
    "tag:deadlift-server": ["group:ops", "autogroup:admin"],
    "tag:deadlift-svc": ["autogroup:admin"]
  },
  "autoApprovers": {
    "routes": {"10.0.0.0/8": ["tag:router"]},
    // Any Server may serve any App's Service, with no approval by hand.
    "services": {
      "tag:deadlift-svc": ["tag:deadlift-server"],
    }
  }
}
"""
    )


def test_an_entry_the_policy_has_laid_out_otherwise_is_not_added_again(tmp_path: Path) -> None:
    policy = """{
	"grants": [
		{"src": ["tag:deadlift-ci-shop"], "dst": ["tag:deadlift-server"], "ip": ["tcp:22"]},
	],
}
"""

    merged = merge(
        tmp_path,
        policy,
        '{"grants": [{"ip": ["tcp:22"], "dst": ["tag:deadlift-server"],'
        ' "src": ["tag:deadlift-ci-shop"]}]}',
    )

    assert merged.status == 0, merged.stderr
    assert merged.stdout == policy


def test_matches_sections_whatever_their_case(tmp_path: Path) -> None:
    policy = """{
	"TagOwners": {
		"tag:ops": ["autogroup:admin"],
	},
}
"""

    merged = merge(tmp_path, policy, '{"tagOwners": {"tag:deadlift-ci-shop": ["autogroup:admin"]}}')

    assert merged.status == 0, merged.stderr
    assert (
        merged.stdout
        == """{
	"TagOwners": {
		"tag:ops": ["autogroup:admin"],
		"tag:deadlift-ci-shop": ["autogroup:admin"],
	},
}
"""
    )


def test_merges_several_entries_files_in_turn(tmp_path: Path) -> None:
    merged = merge(
        tmp_path,
        "{\n}\n",
        '{"tagOwners": {"tag:deadlift-svc": ["group:ops"]}}',
        '{"tagOwners": {"tag:deadlift-ci-shop": ["group:ops"]}}',
    )

    assert merged.status == 0, merged.stderr
    assert (
        merged.stdout
        == """{
	"tagOwners": {"tag:deadlift-svc": ["group:ops"], "tag:deadlift-ci-shop": ["group:ops"]},
}
"""
    )


def test_adds_a_comma_where_the_policy_ends_without_one(tmp_path: Path) -> None:
    policy = """{
    "grants": [
        {"src": ["autogroup:member"], "dst": ["tag:web"], "ip": ["tcp:443"]}
    ],
    "ssh": []
}
"""
    entries = """{
  "grants": [{"src": ["tag:deadlift-ci-shop"], "dst": ["tag:deadlift-server"], "ip": ["tcp:22"]}],
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
    assert (
        merged.stdout
        == """{
    "grants": [
        {"src": ["autogroup:member"], "dst": ["tag:web"], "ip": ["tcp:443"]},
        {"src": ["tag:deadlift-ci-shop"], "dst": ["tag:deadlift-server"], "ip": ["tcp:22"]}
    ],
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
    )


def only_added(before: str, after: str) -> list[str]:
    """The lines ``after`` adds to ``before``, failing if it changes or drops any line other
    than by adding a comma at its end."""
    import difflib

    added: list[str] = []
    old, new = before.split("\n"), after.split("\n")
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new).get_opcodes():
        if tag == "insert":
            added += new[j1:j2]
        elif tag == "replace":
            assert i2 - i1 <= j2 - j1, (old[i1:i2], new[j1:j2])
            for k, line in enumerate(old[i1:i2]):
                assert new[j1 + k] in (line, f"{line},"), (line, new[j1 + k])
            added += new[j1 + i2 - i1 : j2]
        else:
            assert tag == "equal", (tag, old[i1:i2])
    return added


def test_keeps_the_provision_wizards_comments_and_the_policys_own(tmp_path: Path) -> None:
    provision = (ROOT / "cli" / "tailscale-policy.hujson").read_text(encoding="utf-8")
    body = [line for line in provision.split("\n") if not line.startswith("//")]

    merged = merge(tmp_path, DEFAULT_LIKE, "\n".join(body), APP_ENTRIES)

    assert merged.status == 0, merged.stderr
    added = [line.strip() for line in only_added(DEFAULT_LIKE, merged.stdout)]
    for line in body:
        if line.strip().startswith("//"):
            assert line.strip() in added, line
    again = merge(tmp_path, merged.stdout, "\n".join(body), APP_ENTRIES)
    assert again.stdout == merged.stdout


def test_refuses_a_policy_that_isnt_hujson(tmp_path: Path) -> None:
    merged = merge(
        tmp_path,
        '{\n\t"grants": [\n\t\t{"src": ["*"]}\n\t\t{"dst": ["*"]},\n\t],\n}\n',
        APP_ENTRIES,
    )

    assert merged.status == 2
    assert merged.stdout == ""
    assert merged.stderr.startswith("deadlift: ")
    assert "line 4" in merged.stderr


def test_refuses_an_entry_that_contradicts_the_policy(tmp_path: Path) -> None:
    merged = merge(
        tmp_path,
        '{\n\t"randomizeClientPort": false,\n}\n',
        '{"randomizeClientPort": true}',
    )

    assert merged.status == 2
    assert merged.stdout == ""
    assert "randomizeClientPort" in merged.stderr
    assert "Nothing was merged" in merged.stderr


def test_sets_each_new_section_off_with_a_blank_line(tmp_path: Path) -> None:
    merged = merge(
        tmp_path,
        '{\n\t"ssh": [],\n}\n',
        '{"tagOwners": {"tag:deadlift-server": ["autogroup:admin"]},'
        ' "grants": [{"src": ["autogroup:member"], "dst": ["tag:deadlift-server"],'
        ' "ip": ["tcp:22"]}]}',
    )

    assert merged.status == 0, merged.stderr
    assert (
        merged.stdout
        == """{
	"ssh": [],

	"tagOwners": {"tag:deadlift-server": ["autogroup:admin"]},

	"grants": [{"src": ["autogroup:member"], "dst": ["tag:deadlift-server"], "ip": ["tcp:22"]}],
}
"""
    )


def test_opens_up_an_empty_section_on_one_line_for_an_entry_on_several(tmp_path: Path) -> None:
    entries = """{
	"ssh": [
		{
			"action": "accept",
			"src": ["tag:deadlift-ci-shop"],
		},
	],
}
"""

    merged = merge(tmp_path, '{\n\t"ssh": [],\n\t"tests": [ ],\n}\n', entries)

    assert merged.status == 0, merged.stderr
    assert merged.stdout == """{
	"ssh": [
		{
			"action": "accept",
			"src": ["tag:deadlift-ci-shop"],
		},
	],
	"tests": [ ],
}
"""
