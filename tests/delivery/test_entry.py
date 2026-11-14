"""The entry command: what an App's CI can and can't make the Server do, whatever it sends."""

import io
import re
import shutil
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.support.harness import (
    Directory,
    Harness,
    ImageKind,
    Result,
    ServeMapping,
    Symlink,
    docker,
    example_template,
    get,
    matches,
    tar_of,
)

pytestmark = pytest.mark.usefixtures("remove_servers_after_each")


@pytest.fixture(scope="module")
def scratch() -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix="deadlift-test-entry-"))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def marker(scratch: Path) -> Path:
    return scratch / "delivered-code-ran"


NOT_ONLY_THE_TEMPLATE: list[tuple[str, Callable[[Path], bytes]]] = [
    ("bytes that aren't a tar", lambda _: b"not a tar archive\n" * 64),
    ("an empty stdin", lambda _: b""),
    (
        "a deploy script beside the template",
        lambda scratch: tar_of(
            {
                "compose.yaml": example_template(),
                "deploy.sh": f"#!/bin/sh\ntouch {marker(scratch)}\n",
            }
        ),
    ),
    ("a directory", lambda _: tar_of({"compose.yaml": example_template(), "extra": Directory()})),
    ("a symlink as its template", lambda _: tar_of({"compose.yaml": Symlink("/etc/passwd")})),
]


class TestADeliveryThatIsntATarOfOnlyComposeYamlAndTheToken:
    @pytest.mark.parametrize(
        "delivery",
        [delivery for _, delivery in NOT_ONLY_THE_TEMPLATE],
        ids=[name for name, _ in NOT_ONLY_THE_TEMPLATE],
    )
    def test_is_refused_exit_2_with_nothing_run(
        self, harness: Harness, scratch: Path, delivery: Callable[[Path], bytes]
    ) -> None:
        server = harness.server()
        app = server.apps["a"].name

        result = server.send(
            f"deadlift-{app}",
            ["--protocol", "1", "deploy", "feature-1", "--image", f"web={harness.image()}"],
            delivery(scratch),
        )

        assert result.code == 2, result.output
        assert matches(
            r"^deadlift: refused the delivery: .*Nothing was run\.$", result.output, re.M
        )
        assert not marker(scratch).exists()
        assert server.containers(app) == ""
        assert server.tailscale_calls() == []


def compressed_tar_of(template: bytes, *, owner: str) -> bytes:
    """A gzipped tar holding `template` as compose.yaml, owned by `owner`: far smaller sent than
    unpacked."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo("compose.yaml")
        member.size = len(template)
        member.uname = member.gname = owner
        archive.addfile(member, io.BytesIO(template))
    return buffer.getvalue()


@pytest.mark.parametrize("owner", ["runner", "a b"], ids=["any owner", "an owner with a space"])
def test_a_delivery_that_unpacks_past_the_limit_is_refused_exit_2_however_its_sent(
    harness: Harness, owner: str
) -> None:
    server = harness.server()
    app = server.apps["a"].name
    # 2 MiB of template that gzip makes a few kilobytes.
    template = example_template().encode() + b"#" * (2 * 1024 * 1024) + b"\n"
    delivery = compressed_tar_of(template, owner=owner)
    assert len(delivery) < 1024 * 1024

    result = server.send(
        f"deadlift-{app}",
        ["--protocol", "1", "deploy", "feature-1", "--image", f"web={harness.image()}"],
        delivery,
    )

    assert result.code == 2, result.output
    assert matches(r"^deadlift: refused the delivery: it unpacks to over", result.output, re.M)
    assert server.containers(app) == ""
    assert list((server.state / "deliveries").iterdir()) == []


@dataclass(frozen=True)
class Sent:
    result: Result
    containers: str


class TestTheDeliveryProtocol:
    @staticmethod
    def send_with(harness: Harness, protocol: list[str]) -> Sent:
        server = harness.server()
        app = server.apps["a"].name
        result = server.send(
            f"deadlift-{app}",
            [*protocol, "deploy", "feature-1", "--image", f"web={harness.image()}"],
            tar_of({"compose.yaml": example_template()}),
        )
        return Sent(result, server.containers(app))

    def test_one_the_server_doesnt_support_is_refused_exit_2_saying_to_re_run_bootstrap(
        self, harness: Harness
    ) -> None:
        sent = self.send_with(harness, ["--protocol", "2"])

        assert sent.result.code == 2, sent.result.output
        assert matches(
            r"^deadlift: .*protocol '2'.*re-run `deadlift server bootstrap`",
            sent.result.output,
            re.I | re.M,
        )
        assert sent.containers == ""

    def test_none_at_all_is_refused_exit_2(self, harness: Harness) -> None:
        sent = self.send_with(harness, [])

        assert sent.result.code == 2, sent.result.output
        assert matches(r"^deadlift: refused the delivery: .*protocol", sent.result.output, re.M)
        assert sent.containers == ""


def test_nothing_in_a_delivery_is_executed_the_token_included(
    harness: Harness, scratch: Path
) -> None:
    server = harness.server()
    app = server.apps["a"].name
    ran = scratch / "token-ran"

    result = server.deliver(
        app,
        ["deploy", "feature-1", "--image", f"web={harness.image()}"],
        token=f"$(touch {ran})`touch {ran}`",
    )

    assert result.outcome == "done", result.output
    assert not ran.exists()


CROSSINGS: list[tuple[str, Callable[[str], list[str]]]] = [
    ("--app", lambda a: ["--app", a]),
    ("an Environment outside its own", lambda a: [f"../{a}/feature-1"]),
]


class TestTheAppIsWorkedOutFromSudoUser:
    def test_another_apps_deploy_user_deploying_the_same_key_gets_its_own_environment(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        a, b = server.apps["a"].name, server.apps["b"].name
        server.deliver(a, ["deploy", "feature-1", "--image", f"web={harness.image('healthy', 1)}"])

        result = server.deliver(
            b, ["deploy", "feature-1", "--image", f"web={harness.image('healthy', 2)}"]
        )

        assert result.outcome == "done", result.output
        assert result.url == f"https://{b}.tailtest.ts.net:10001/"
        assert get(server.slot_port("a", 1), "/version") == "1"
        assert get(server.slot_port("b", 1), "/version") == "2"
        mappings = server.serve_mappings()
        assert ServeMapping(f"svc:{a}", 10001, f"http://127.0.0.1:{server.slot_port('a', 1)}") in (
            mappings
        )
        assert ServeMapping(f"svc:{b}", 10001, f"http://127.0.0.1:{server.slot_port('b', 1)}") in (
            mappings
        )

    @pytest.mark.parametrize(
        "extra", [extra for _, extra in CROSSINGS], ids=[name for name, _ in CROSSINGS]
    )
    def test_arguments_naming_another_app_are_refused_exit_2(
        self, harness: Harness, extra: Callable[[str], list[str]]
    ) -> None:
        server = harness.server()
        a, b = server.apps["a"].name, server.apps["b"].name
        server.deliver(a, ["deploy", "feature-1", "--image", f"web={harness.image()}"])
        before = server.serve_mappings()

        args = extra(a)
        image = f"web={harness.image('healthy', 2)}"
        result = server.deliver(
            b,
            ["deploy", *args, "--image", image]
            if args[0].startswith("..")
            else ["deploy", "feature-1", "--image", image, *args],
        )

        assert result.code == 2, result.output
        assert matches(r"^deadlift: ", result.reason)
        assert get(server.slot_port("a", 1), "/version") == "1"
        assert server.serve_mappings() == before
        assert server.containers(b) == ""

    def test_an_image_another_apps_deploy_left_on_the_server_runs_only_if_the_registry_gives_it(
        self, harness: Harness
    ) -> None:
        server = harness.server()
        a, b = server.apps["a"].name, server.apps["b"].name
        assert server.deliver(
            b, ["deploy", "dev", "--image", f"web={harness.image()}"]
        ).outcome == ("done")
        # b's private image: on the Server since b's deploy pulled it with b's token, and not
        # one the registry gives a's.
        private = f"{harness.repository}:{b}-private"
        docker("image", "tag", harness.image(), private)

        result = server.deliver(a, ["deploy", "feature-1", "--image", f"web={private}"])

        assert result.code == 1, result.output
        assert matches(rf"^deadlift: couldn't pull {re.escape(private)}", result.reason)
        assert server.containers(a) == ""

    @pytest.mark.parametrize("user", ["deadlift-nobody", "root"])
    def test_a_user_that_isnt_an_apps_deploy_user_is_refused_exit_2(
        self, harness: Harness, user: str
    ) -> None:
        server = harness.server()

        result = server.send(
            user,
            ["--protocol", "1", "deploy", "feature-1", "--image", f"web={harness.image()}"],
            tar_of({"compose.yaml": example_template()}),
        )

        assert result.code == 2, result.output
        assert matches(
            r"^deadlift: refused the delivery: .*isn't the deploy user", result.output, re.M
        )
        assert server.tailscale_calls() == []


def test_operations_on_one_server_run_one_at_a_time(harness: Harness) -> None:
    server = harness.server()
    app = server.apps["a"].name
    ref = harness.image()
    release = server.hold_lock()

    with ThreadPoolExecutor(1) as pool:
        delivery = pool.submit(
            server.deliver, app, ["deploy", "feature-1", "--image", f"web={ref}"]
        )
        time.sleep(3)
        while_held = (delivery.done(), server.containers(app))
        release()
        result = delivery.result()

    assert while_held == (False, "")
    assert result.outcome == "done", result.output


def test_a_deploy_with_no_free_slot_is_skipped_exit_75_and_deliver_doesnt_fail_the_step(
    harness: Harness,
) -> None:
    server = harness.server(slots=1)
    app = server.apps["a"].name
    ref = harness.image()
    server.deliver(app, ["deploy", "feature-1", "--image", f"web={ref}"])

    result = server.deliver(app, ["deploy", "feature-2", "--image", f"web={ref}"])

    assert result.code == 0, result.output
    assert result.outcome == "skipped"
    assert matches(r"^deadlift: .*held, by feature-1, so feature-2 wasn't deployed", result.reason)
    direct = server.send(
        f"deadlift-{app}",
        ["--protocol", "1", "deploy", "feature-2", "--image", f"web={ref}"],
        tar_of({"compose.yaml": example_template()}),
    )
    assert direct.code == 75, direct.output


# Exit codes as the entry command gives them, before deliver maps them to outcomes.
@pytest.mark.parametrize(
    ("kind", "code"), [("healthy", 0), ("failing-migration", 1)], ids=["done", "failed"]
)
def test_the_entry_command_exits_as_documented_when(
    harness: Harness, kind: ImageKind, code: int
) -> None:
    server = harness.server()
    app = server.apps["a"].name

    result = server.send(
        f"deadlift-{app}",
        ["--protocol", "1", "deploy", "feature-1", "--image", f"web={harness.image(kind)}"],
        tar_of({"compose.yaml": example_template()}),
    )

    assert result.code == code, result.output
    assert matches(r"^deadlift: ", result.output.rstrip().split("\n")[-1])
