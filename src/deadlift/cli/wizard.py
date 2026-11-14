"""A wizard walks a human through a manual procedure, step by step: the library the provision
wizard (deadlift.cli.provision) is built on, ported from the /wizard skill's Bash library, with
the same look and the same prompts.

Each stage clears the screen (only when stdout is a terminal, so piped logs stay readable),
shows its progress, and says what the human does in a browser or terminal. Answers they type are
kept in an env file, written as ``KEY='value'`` lines, and offered again on a later run, so they
can stop with Ctrl-C any time and run it again to carry on.

Input is read as Bash's ``read`` reads it: from fd 0 a byte at a time (with Readline when it's a
terminal, for ``ask``), so nothing typed ahead is held back from a child that reads it, and with
the spaces and tabs around an answer dropped.
"""

import os
import re
import shutil
import sys
from pathlib import Path

from deadlift.cli.common import maintainer_env
from deadlift.core.process import Stream, run

_IFS_WHITESPACE = " \t\n"


def _tput(*capability: str) -> str:
    """What ``tput`` prints for ``capability``, as Bash's ``$(tput …)`` took it."""
    done = run(
        ["tput", *capability], env=maintainer_env(), stdout=Stream.CAPTURE, stderr=Stream.DEVNULL
    )
    return done.stdout.rstrip("\n") if done.ok else ""


def _has_colours() -> bool:
    if not sys.stdout.isatty() or shutil.which("tput") is None:
        return False
    colours = _tput("colors").strip()
    return colours.isdigit() and int(colours) >= 8


def read_line() -> str | None:
    """A line from stdin, read a byte at a time, without its line break: None at the end of
    input with nothing read, as Bash's ``read`` fails then."""
    data = bytearray()
    while True:
        byte = os.read(0, 1)
        if not byte:
            if not data:
                return None
            break
        if byte == b"\n":
            break
        data += byte
    return data.decode("utf-8", errors="surrogateescape")


def read_answer() -> str | None:
    """A line from stdin as ``read -r`` gives it to one variable: spaces and tabs around it
    dropped. None at the end of input with nothing read."""
    line = read_line()
    return None if line is None else line.strip(_IFS_WHITESPACE)


def decode_saved(value: str) -> str | None:
    """A value as an env file holds it, with :func:`encode_saved`'s single quotes or a plain
    dotenv double-quoted value undone. None when it can't be decoded safely (escapes, ``$`` or
    ``"`` inside double quotes), so the human is asked again."""
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("'\\''", "'")
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        inner = value[1:-1]
        return None if any(char in inner for char in '\\$"') else inner
    return value


def encode_saved(key: str, value: str) -> str:
    """The env file's line for ``key``: ``KEY='value'``, with each ``'`` written ``'\\''``."""
    escaped = value.replace("'", "'\\''")
    return f"{key}='{escaped}'"


def saved_value(lines: list[str], key: str) -> str | None:
    """``key``'s value among an env file's ``lines``: the last line for it wins."""
    found = [line for line in lines if re.match(f"^{key}=", line)]
    if not found:
        return None
    return decode_saved(found[-1].partition("=")[2])


class Wizard:
    """One run of a wizard: its stages, its answers file and what's left to do by hand."""

    def __init__(self, total_stages: int, env_file: Path) -> None:
        self.total_stages = total_stages
        self.env_file = env_file
        self.stage_index = 0
        self.written_env: list[str] = []
        """Keys written to the env file this run."""
        self.skipped: list[str] = []
        """Things the human still has to do by hand."""
        if _has_colours():
            self.bold, self.dim, self.reset = _tput("bold"), _tput("dim"), _tput("sgr0")
            self.blue, self.green = _tput("setaf", "4"), _tput("setaf", "2")
            self.yellow = _tput("setaf", "3")
        else:
            self.bold = self.dim = self.reset = self.blue = self.green = self.yellow = ""

    def _out(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def clear(self) -> None:
        """Wipes the terminal so only the current step is on screen; nothing when stdout isn't
        a terminal."""
        if not sys.stdout.isatty():
            return
        sys.stdout.flush()
        if shutil.which("tput") is not None:
            cleared = run(["tput", "clear"], env=maintainer_env(), stdout=Stream.INHERIT)
            if cleared.ok:
                return
        self._out("\033[2J\033[3J\033[H")

    def banner(self, title: str) -> None:
        """The opening frame: what this wizard does."""
        self.clear()
        self._out(f"\n{self.bold}{self.blue}  {title}{self.reset}\n")
        self._out(f"{self.dim}  {self.total_stages} stages{self.reset}\n\n")
        self._out(
            f"{self.dim}  You drive the browser; this wizard tells you exactly what to do and\n"
        )
        self._out("  captures the values you copy back. Stop any time with Ctrl-C and re-run\n")
        self._out(f"  later, since it remembers values already saved.{self.reset}\n")
        self.pause("Ready to start?")

    def stage(self, name: str) -> None:
        """Clears the screen, then announces a stage and shows progress."""
        self.clear()
        self.stage_index += 1
        self._out(
            f"\n{self.bold}{self.blue}▸ Stage {self.stage_index}/{self.total_stages}"
            f" · {name}{self.reset}\n"
        )

    def say(self, text: str) -> None:
        """A plain instruction line."""
        self._out(f"  {text}\n")

    def step(self, text: str) -> None:
        """An action the human takes in the browser."""
        self._out(f"  {self.blue}•{self.reset} {text}\n")

    def note(self, text: str) -> None:
        self._out(f"  {self.dim}{text}{self.reset}\n")

    def warn(self, text: str) -> None:
        self._out(f"  {self.yellow}⚠ {text}{self.reset}\n")

    def done(self, text: str) -> None:
        """A check that passed."""
        self._out(f"  {self.green}✓{self.reset} {text}\n")

    def open_url(self, url: str) -> None:
        """Opens ``url`` in the human's browser, cross-platform incl. WSL."""
        self._out(f"  {self.green}↗ opening{self.reset} {url}\n")
        for opener in ("wslview", "explorer.exe", "xdg-open", "open"):
            if shutil.which(opener) is not None:
                opened = run(
                    [opener, url],
                    env=maintainer_env(),
                    stdout=Stream.DEVNULL,
                    stderr=Stream.DEVNULL,
                )
                if opened.ok:
                    return
                break
        self.warn(f"couldn't open a browser, so visit it manually: {url}")

    def pause(self, message: str = "Press Enter to continue") -> None:
        """Waits for the human to confirm they've done the manual part."""
        self._out(f"  {self.dim}{message}{self.reset} ")
        read_answer()

    def confirm(self, question: str) -> bool:
        """A y/N gate: True on yes."""
        self._out(f"  {self.yellow}? {question} [y/N]{self.reset} ")
        reply = read_answer() or ""
        return reply[:1] in ("y", "Y")

    def existing(self, key: str) -> str:
        """``key``'s value in the env file, or "" when it has none."""
        try:
            text = self.env_file.read_text(encoding="utf-8", errors="surrogateescape")
        except OSError:
            return ""
        return saved_value(text.split("\n"), key) or ""

    def ask(self, key: str, prompt: str) -> str | None:
        """Reads a visible (non-secret) answer, edited with Readline on a terminal. Offers the
        env file's value for ``key`` on re-runs: Enter keeps it. None at the end of input with
        nothing typed."""
        current = self.existing(key)
        answer = self._read_with_prompt(prompt, current)
        if answer is None:
            return None
        return current if not answer and current else answer

    def _read_with_prompt(self, prompt: str, current: str) -> str | None:
        # As Bash's `read -e -p`: the prompt shows only when input comes from a terminal.
        if not sys.stdin.isatty():
            return read_answer()
        start, end = "\001", "\002"
        if sys.stdout.isatty():
            try:
                import readline  # noqa: PLC0415 (only for a human at a terminal)
            except ImportError:
                start = end = ""
            else:
                # libedit (as on macOS) may show Readline's markers as they are.
                if "libedit" in (readline.__doc__ or ""):
                    start = end = ""
        text = f"  {start}{self.bold}{end}{prompt}{start}{self.reset}{end} "
        if current:
            text += f"{start}{self.dim}{end}[Enter keeps current]{start}{self.reset}{end} "
        if not sys.stdout.isatty():
            sys.stderr.write(text)
            sys.stderr.flush()
            return read_answer()
        try:
            return input(text).strip(_IFS_WHITESPACE)
        except EOFError:
            return None

    def write_env(self, key: str, value: str) -> None:
        """Puts ``KEY='value'`` in the env file in place of any line for ``key``. Creates the
        file 0600; writes through a symlink and keeps an existing file's mode."""
        if not self.env_file.exists():
            os.close(os.open(self.env_file, os.O_WRONLY | os.O_CREAT, 0o600))
        text = self.env_file.read_text(encoding="utf-8", errors="surrogateescape")
        lines = text.split("\n")
        if lines[-1] == "":
            lines.pop()
        kept = [line for line in lines if not re.match(f"^{key}=", line)]
        kept.append(encode_saved(key, value))
        with self.env_file.open("w", encoding="utf-8", errors="surrogateescape") as file:
            file.write("".join(f"{line}\n" for line in kept))
        self.written_env.append(key)
        self._out(f"  {self.green}✓ wrote{self.reset} {key} → {self.env_file}\n")

    def finish(self) -> None:
        """Clears, then shows a closing summary of everything done."""
        self.clear()
        self._out(f"\n{self.bold}{self.green}  ✓ Setup complete{self.reset}\n")
        if self.written_env:
            self.note(
                f"wrote {len(self.written_env)} value(s) to {self.env_file}:"
                f" {' '.join(self.written_env)}"
            )
        if self.skipped:
            self._out("\n")
            self.warn("still to do by hand:")
            for skipped in self.skipped:
                self.note(f"  - {skipped}")
        self._out("\n")
