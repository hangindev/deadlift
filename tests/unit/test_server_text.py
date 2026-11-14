"""The Server's small text conventions, as its Bash read them: name=value files, a command's
last line of output, and sentences naming several things."""

from deadlift.server.text import last_line, read_settings_text, sentence


def test_a_settings_file_gives_each_line_name_and_value() -> None:
    assert read_settings_text("port_base=20000\nslots=3\n") == [
        ("port_base", "20000"),
        ("slots", "3"),
    ]


def test_a_value_keeps_its_own_equals_signs_but_not_one_trailing_one() -> None:
    # As Bash's `IFS='=' read -r key value` splits a line.
    assert read_settings_text("a=b=c\na=b=\na=\nabc\n=x\na=b==\na===\n") == [
        ("a", "b=c"),
        ("a", "b"),
        ("a", ""),
        ("abc", ""),
        ("", "x"),
        ("a", "b=="),
        ("a", "=="),
    ]


def test_a_last_line_without_its_line_break_is_read_too() -> None:
    assert read_settings_text("x=1\ny=2") == [("x", "1"), ("y", "2")]
    assert read_settings_text("") == []


def test_last_line_is_what_tail_gives_without_the_line_break() -> None:
    assert last_line("one\ntwo\n") == "two"
    assert last_line("one\ntwo") == "two"
    assert last_line("one\n\n") == ""
    assert last_line("") == ""


def test_a_sentence_joins_names_with_commas_and_a_last_and() -> None:
    assert sentence([]) == ""
    assert sentence(["a"]) == "a"
    assert sentence(["a", "b"]) == "a and b"
    assert sentence(["a", "b", "c"]) == "a, b and c"
