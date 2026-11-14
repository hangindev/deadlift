"""What the entry command lets through of a delivery, read from the tar itself."""

import io
import tarfile
from pathlib import Path
from typing import Literal

import pytest

from deadlift.core.outcome import Exit, Stop
from deadlift.server.entry import read_delivery


def archive(
    tmp_path: Path,
    *members: tuple[str, bytes] | tarfile.TarInfo,
    mode: Literal["w", "w:gz", "w:xz"] = "w",
    owner: str = "runner",
) -> Path:
    path = tmp_path / "archive.tar"
    with tarfile.open(path, mode) as tar:
        for member in members:
            if isinstance(member, tarfile.TarInfo):
                tar.addfile(member)
                continue
            name, content = member
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.uname = info.gname = owner
            tar.addfile(info, io.BytesIO(content))
    return path


def refusal(path: Path) -> str:
    with pytest.raises(Stop) as stopped:
        read_delivery(path)
    assert stopped.value.code == Exit.REFUSED
    return str(stopped.value.why)


def test_a_template_and_a_token_get_through(tmp_path: Path) -> None:
    delivered = read_delivery(
        archive(tmp_path, ("compose.yaml", b"services: {}\n"), ("./token", b"t"))
    )
    assert delivered == {"compose.yaml": b"services: {}\n", "token": b"t"}
    assert read_delivery(archive(tmp_path)) == {}


def test_anything_else_is_refused(tmp_path: Path) -> None:
    assert "it holds 'deploy.sh'" in refusal(
        archive(tmp_path, ("compose.yaml", b"x"), ("deploy.sh", b"x"))
    )
    assert "it holds token twice" in refusal(archive(tmp_path, ("token", b"1"), ("./token", b"2")))
    link = tarfile.TarInfo("compose.yaml")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    assert "something other than plain files" in refusal(archive(tmp_path, link))
    (tmp_path / "junk").write_bytes(b"not a tar archive\n" * 64)
    assert "it isn't a tar archive" in refusal(tmp_path / "junk")


@pytest.mark.parametrize("owner", ["runner", "a b"])
@pytest.mark.parametrize("mode", ["w", "w:gz", "w:xz"])
def test_what_unpacks_past_the_limit_is_refused_whatever_its_owner_or_compression(
    tmp_path: Path, owner: str, mode: Literal["w", "w:gz", "w:xz"]
) -> None:
    big = archive(
        tmp_path, ("compose.yaml", b"#" * 1048000), ("token", b"t" * 1000), mode=mode, owner=owner
    )
    assert "unpacks to over 1048576 bytes" in refusal(big)
