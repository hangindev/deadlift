"""The maintainer's computer and the world around it, for ``deadlift server provision``: machines
they can buy, each answering SSH on its public address until its firewall closes it, and the
tailnet those machines join. The fakes are test/fakes/provision/ (``ssh``, ``tailscale`` and a
browser opener); a World is their directory, ``$DEADLIFT_TEST_WORLD``.

``World.provision`` runs the wizard with a scripted human: one ``Turn`` per thing they do, each
waiting until the wizard has printed what it's waiting for."""

import codecs
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO

REPO = Path(__file__).resolve().parents[2]
DEADLIFT = REPO / "bin/deadlift"
FAKES = REPO / "test/fakes/provision"

# How long the human waits for each prompt, and for the wizard to finish after the last one.
TURN_TIMEOUT = 20.0
RUN_TIMEOUT = 300.0


@dataclass(frozen=True)
class Turn:
    """One thing the human does: once the wizard has printed ``on`` (a regular expression), do
    ``then``, and type ``answer`` (or interrupt the wizard with Ctrl-C)."""

    on: str
    answer: str | None = None
    then: Callable[[], None] | None = None
    interrupt: bool = False


@dataclass(frozen=True)
class Run:
    """How the wizard exited (negative: by that signal) and everything it printed, stdout and
    stderr interleaved as they came."""

    code: int
    output: str


@dataclass(frozen=True)
class Tailnet:
    """A machine's name and tags on the tailnet."""

    name: str
    tags: list[str]


class Transcript:
    """What a child prints on its stdout and stderr, merged in the order it arrives."""

    def __init__(self) -> None:
        self._changed = threading.Condition()
        self._text = ""
        self._open = 0

    @property
    def text(self) -> str:
        with self._changed:
            return self._text

    def follow(self, stream: IO[bytes]) -> threading.Thread:
        """Reads ``stream`` to its end in the background."""
        with self._changed:
            self._open += 1
        thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        thread.start()
        return thread

    def _pump(self, stream: IO[bytes]) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            chunk = os.read(stream.fileno(), 65536)
            text = decoder.decode(chunk, final=not chunk)
            with self._changed:
                self._text += text
                if not chunk:
                    self._open -= 1
                self._changed.notify_all()
            if not chunk:
                return

    def wait_for(self, pattern: str, start: int, timeout: float) -> re.Match[str] | str:
        """The first match of ``pattern`` in what was printed from ``start`` on, or why there
        is none: the child closed its output first, or ``timeout`` seconds passed."""
        expression = re.compile(pattern)
        with self._changed:
            found: re.Match[str] | None = None

            def matched_or_closed() -> bool:
                nonlocal found
                found = expression.search(self._text, start)
                return found is not None or self._open == 0

            if not self._changed.wait_for(matched_or_closed, timeout):
                return "timed out"
            return found if found is not None else "the wizard exited"


@dataclass(frozen=True)
class World:
    dir: Path

    def _machine(self, address: str) -> Path:
        return self.dir / "machines" / address

    @staticmethod
    def _read(path: Path) -> str:
        return path.read_text() if path.exists() else ""

    def _lines(self, path: Path) -> list[str]:
        return [line for line in self._read(path).split("\n") if line]

    def buy(self, address: str) -> None:
        """A fresh Ubuntu 24.04 machine bought at the provider, at a public IPv4 address."""
        self._machine(address).mkdir()

    def close_public_ssh(self, address: str) -> None:
        """Blocks SSH on a machine's public address, as the provider's firewall does."""
        (self._machine(address) / "public-ssh-closed").write_text("")

    def bootstraps(self, address: str) -> list[str]:
        """The routes each bootstrap of the machine came by, in order: public or tailnet."""
        return self._lines(self._machine(address) / "bootstraps")

    def tailnet(self, address: str) -> Tailnet:
        """The machine's name and tags on the tailnet, once it has joined."""
        machine = self._machine(address)
        return Tailnet(self._read(machine / "hostname").strip(), self._lines(machine / "tags"))

    def opened(self) -> list[str]:
        """The URLs the wizard opened in the browser."""
        return self._lines(self.dir / "opened")

    def provision(self, turns: Sequence[Turn], args: Sequence[str] = ()) -> Run:
        """Runs ``deadlift server provision [args]``, with the human taking each turn in order."""
        child = subprocess.Popen(
            [str(DEADLIFT), "server", "provision", *args],
            env={
                "PATH": f"{self.dir / 'bin'}:/usr/local/bin:/usr/bin:/bin",
                "HOME": str(self.dir / "home"),
                "LANG": "C.UTF-8",
                "DEADLIFT_TEST_WORLD": str(self.dir),
            },
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert child.stdin and child.stdout and child.stderr
        transcript = Transcript()
        readers = [transcript.follow(child.stdout), transcript.follow(child.stderr)]
        try:
            cursor = 0
            for turn in turns:
                found = transcript.wait_for(turn.on, cursor, TURN_TIMEOUT)
                if isinstance(found, str):
                    child.kill()
                    raise AssertionError(
                        f"{found} waiting for /{turn.on}/ after:\n{transcript.text[cursor:]}"
                    )
                cursor = found.end()
                if turn.then:
                    turn.then()
                if turn.interrupt:
                    child.send_signal(signal.SIGINT)
                    break
                # If it has exited, the next turn says so.
                with suppress(BrokenPipeError):
                    child.stdin.write(f"{turn.answer or ''}\n".encode())
                    child.stdin.flush()
            with suppress(BrokenPipeError):
                child.stdin.close()
            code = child.wait(RUN_TIMEOUT)
            for reader in readers:
                reader.join(RUN_TIMEOUT)
            return Run(code, transcript.text)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            for stream in (child.stdout, child.stderr):
                stream.close()


@contextmanager
def world_at() -> Generator[World]:
    """A fresh World in a temporary directory, removed afterwards."""
    path = Path(tempfile.mkdtemp(prefix="deadlift-provision-"))
    try:
        (path / "bin").mkdir()
        (path / "machines").mkdir()
        (path / "home").mkdir()
        for tool in ("ssh", "tailscale"):
            (path / "bin" / tool).symlink_to(FAKES / tool)
        for opener in ("xdg-open", "open", "wslview", "explorer.exe"):
            (path / "bin" / opener).symlink_to(FAKES / "browser")
        yield World(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)
