"""The wizard library's answers file and the provision wizard's reading of what it prints."""

from pathlib import Path

import pytest

from deadlift.cli.provision import magic_dns_suffix, policy_entries
from deadlift.cli.wizard import Wizard, decode_saved, encode_saved, saved_value


@pytest.mark.parametrize(
    ("value", "line"),
    [
        ("deadlift-1", "KEY='deadlift-1'"),
        ("", "KEY=''"),
        ("it's", "KEY='it'\\''s'"),
        ('a "b" $c \\d', "KEY='a \"b\" $c \\d'"),
    ],
)
def test_an_answer_is_saved_single_quoted_and_read_back(value: str, line: str) -> None:
    assert encode_saved("KEY", value) == line
    assert decode_saved(line.removeprefix("KEY=")) == value


@pytest.mark.parametrize(
    ("saved", "value"),
    [("plain", "plain"), ('"double"', "double"), ("'", "'"), ('"', '"'), ("", "")],
)
def test_a_dotenv_value_is_read_as_given(saved: str, value: str) -> None:
    assert decode_saved(saved) == value


@pytest.mark.parametrize("saved", ['"a\\nb"', '"$HOME"', '"a"b"'])
def test_a_double_quoted_value_with_escapes_is_asked_again(saved: str) -> None:
    assert decode_saved(saved) is None


def test_the_last_line_for_a_key_wins() -> None:
    lines = ["A='1'", "AB='x'", "A='2'", "# A='3'"]

    assert saved_value(lines, "A") == "2"
    assert saved_value(lines, "B") is None


def test_writing_an_answer_replaces_its_line_and_keeps_the_rest(tmp_path: Path) -> None:
    env_file = tmp_path / "provision.env"
    wizard = Wizard(1, env_file)

    wizard.write_env("SERVER_NAME", "deadlift-1")
    wizard.write_env("SERVER_IP", "203.0.113.7")
    wizard.write_env("SERVER_NAME", "deadlift-2")

    assert env_file.read_text() == "SERVER_IP='203.0.113.7'\nSERVER_NAME='deadlift-2'\n"
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert wizard.existing("SERVER_NAME") == "deadlift-2"
    assert wizard.existing("TAILNET") == ""
    assert wizard.written_env == ["SERVER_NAME", "SERVER_IP", "SERVER_NAME"]


def test_writing_an_answer_keeps_the_files_mode_and_writes_through_a_symlink(
    tmp_path: Path,
) -> None:
    target = tmp_path / "real.env"
    target.write_text("OTHER=kept\n")
    target.chmod(0o640)
    link = tmp_path / "provision.env"
    link.symlink_to(target)

    Wizard(1, link).write_env("KEY", "v")

    assert link.is_symlink()
    assert target.read_text() == "OTHER=kept\nKEY='v'\n"
    assert target.stat().st_mode & 0o777 == 0o640


def test_the_policy_is_printed_indented_without_its_top_comments() -> None:
    hujson = '// top\n// more\n{\n\t// inside\n\t"a": 1,\n\n}\n'

    assert policy_entries(hujson) == '    {\n    \t// inside\n    \t"a": 1,\n    \n    }\n'


def test_the_tailnet_dns_name_is_read_from_tailscale_status() -> None:
    status = '{\n  "Self": {},\n  "MagicDNSSuffix": "tail1a2b3c.ts.net",\n  "Peer": {}\n}\n'

    assert magic_dns_suffix(status) == "tail1a2b3c.ts.net"
    assert magic_dns_suffix('{"Self": {}}') == ""
    assert magic_dns_suffix("") == ""


def test_a_confirmation_is_typed_in_the_terminals_own_colour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wizard = Wizard(1, tmp_path / "answers.env")
    wizard.yellow, wizard.reset = "<yellow>", "<reset>"
    monkeypatch.setattr("deadlift.cli.wizard.read_line", lambda: "y")

    assert wizard.confirm("Saved?")
    assert capsys.readouterr().out == "  <yellow>? Saved? [y/N]<reset> "
