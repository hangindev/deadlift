"""leave: takes a CI job off the tailnet once the deliver action is done, as the Tailscale
action's own clean-up would at the end of the job. Logging out removes the ephemeral node at
once, and stopping tailscaled lets a later step in the job join again.
"""

import shutil
import time
from collections.abc import Sequence

from deadlift.actions import job
from deadlift.core.outcome import Exit, fail
from deadlift.core.process import Env, Stream, run

# How long to wait for tailscaled to stop: 20 looks, half a second apart.
_LOOKS = 20
_PAUSE = 0.5


def _running(env: Env) -> bool:
    return run(["pgrep", "-x", "tailscaled"], env=env, stdout=Stream.DEVNULL).ok


def _leave() -> int:
    env = job.job_env()
    # Never installed: the join failed before it got that far.
    if shutil.which("tailscaled", path=env.variables.get("PATH")) is None:
        return int(Exit.DONE)

    if not run(["sudo", "tailscale", "logout"], env=env, stdout=Stream.INHERIT).ok:
        job.say(
            "::warning::deadlift: couldn't log out of the tailnet."
            " The node is removed when the job ends."
        )
    run(["sudo", "pkill", "-x", "tailscaled"], env=env, stdout=Stream.INHERIT)
    for _ in range(_LOOKS):
        if not _running(env):
            break
        time.sleep(_PAUSE)
    if _running(env):
        fail("tailscaled didn't stop, so this job can't join the tailnet again.")
    code = run(["sudo", "tailscaled", "--cleanup"], env=env, stdout=Stream.INHERIT).code
    # As a shell reports a child killed by a signal.
    return 128 - code if code < 0 else code


def main(argv: Sequence[str]) -> int:
    return job.run_job(_leave)
