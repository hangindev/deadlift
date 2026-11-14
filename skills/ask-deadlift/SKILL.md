---
name: ask-deadlift
description: Explains what deadlift did to an App's Environments and why. Use when a deadlift delivery was skipped or failed, when reading a `deadlift: ` reason line or exit code, or when asked how a deadlift operation (deploy, Rollback, Recovery, the Sweep, the watchdog) works.
---

# ask-deadlift

deadlift delivers an App, described by one `compose.yaml`, to a Server on a tailnet, as Environments: the Dev Environment, `dev`, which tracks a branch, and short-lived Feature Environments, `feature-<key>`, one per pull request in the starter workflows. The App's GitHub workflows run each operation through deadlift's `deliver` action, and the Server answers with **reason lines**, each starting `deadlift: `. This skill turns a reason line into what happened, what's running now, and what to do next.

Use deadlift's own words with the maintainer: App, Server, Environment, Slot, Sweep, Volume Snapshot, Recovery, Rollback.

## Diagnose a delivery

1. **Get the reason line, verbatim.** It's the last line starting `deadlift: ` that the run printed. The `deliver` step's job summary shows it with the outcome; the starter Feature workflow also puts it in the pull request's deadlift comment, and a skip's in its "Feature Environment" check. The step's log has everything the Server printed: `gh run view <run-id> --log`, then the `deliver` step. Done when you hold the exact line, or know the run printed none (see the exit codes below).
2. **Read the outcome and exit code** from the table below. Done when you know whether anything changed: a refusal changed nothing.
3. **Match the line in [DIAGNOSES.md](DIAGNOSES.md):** the entry whose quoted line is yours apart from its `<placeholders>`. Done when you have its cause and its fix. No entry matches: read the line as below, and the lines just above it in the log, which are Docker's and Compose's own.
4. **Check the Server** when the line leaves doubt about what's running: `deadlift server status <server>` shows each App's Environments, which containers run, Slots in use, Volume Snapshots, and disk use against the thresholds. It logs in as root, so it's the maintainer's to run; ask them for its output when you can't.
5. **Report** in three parts: what happened, what's running now and at which address, and the next step with who takes it (a fix in the App's code, a re-run, or the maintainer on the Server). Anything that throws data away, such as `--discard-newer-data`, destroying a Feature Environment or `deadlift app remove`, is the maintainer's decision: name it, say what it loses, and ask.

## Outcomes and exit codes

deadlift's exit codes are the same on the Server, in CI and from the maintainer's `deadlift` command. The Server's code reaches CI through `deliver`, which turns it into its `outcome` output and decides whether the step fails:

| Exit | Meaning | `deliver` outcome | The step |
| --- | --- | --- | --- |
| `0` | done | `done` | passes |
| `1` | failed | `failed` | fails |
| `2` | refused: bad usage, or not allowed. Nothing was changed | `failed` | fails |
| `75` | skipped: nothing was done, and that's not a failure | `skipped` | passes |

So a red `deliver` step is a failure or a refusal, and the reason line says which. A skip stays green: read the outcome, not the colour. Other codes:

- `ssh` exiting 255 means the Server was never reached: `deliver` gives its own line, failed, exit 1 (see DIAGNOSES.md, Reaching the Server).
- Any other code, such as 5 from a template the check couldn't read (`template: error: …` in the log) or a process killed partway, is `failed`, often with no reason line. The job summary then says the Server gave no reason: read the step's log.

## Reading a reason line

- **The last one is the reason.** Earlier `deadlift: ` lines in the same run are steps along the way, such as a Sweep's one line per Environment destroyed.
- **What happened, then what's left running.** Each reason ends by saying what state it left: `Nothing was changed`, `Nothing was run` and `it was left as it was` mean it's safe to fix and run again; `Dev is up on its current version`, `It's stopped` and `Dev is stopped` say what a reviewer will find.
- **Text after a colon is often someone else's.** Where a step failed, deadlift quotes the last line Compose or Docker printed: for a one-shot service such as a migration, Compose's `service "<service>" didn't complete successfully: exit <n>`; for the routed service, `container <container> is unhealthy`. "(above)" means the full error is in the lines just above.
- **Lines without the prefix are progress**, such as `Pulling <ref>`, `Stopping dev for a Volume Snapshot`, or `Waiting for another operation on this Server to finish`: one operation at a time runs on a Server, so a run queues behind another rather than failing.
- **Names on the Server:** an Environment's Compose project is `deadlift-<app>-<environment>`, so its containers are `deadlift-<app>-<environment>-<service>-1`.

## The operations

The App owns its workflows, which may have changed since `deadlift init` wrote them: read `.github/workflows/deadlift-*.yml` in the App's repo for what it actually runs and when.

- **`deploy dev`** (starter: `deadlift-dev`, on each push to the tracked branch). Checks the template and pulls every image while Dev keeps running. Then it stops Dev, takes a Volume Snapshot of every named volume, and starts the new version, healthy when the routed service's Compose healthcheck passes. When it doesn't start, **Recovery** brings Dev down, puts the snapshot back and starts the previous version; the reason says whether that worked. Before delivering, the starter skips a commit the branch has moved past.
- **`deploy feature-<key>`** (starter: `deadlift-feature`, on pull request events, key = PR number, skipped by a `skip-deploy` label). Takes a free **Slot**, or the one the key holds, which fixes its URL at a port on the App's address. A redeploy keeps its data. A failed deploy leaves it stopped. Skipped while the disk is over the Server's lower threshold. With `--keep`, which the starter passes as the open pull requests, it first sweeps every other Feature Environment, so closed ones free their Slots.
- **`rollback dev`** (starter: `deadlift-rollback`, run by hand). **Rollback** starts Dev's previous version on today's data and keeps it when healthy, otherwise starts the current version again and refuses. With `--discard-newer-data` (the workflow's tick box) it instead goes back to the Volume Snapshot from before the current version, keeping a snapshot of today's data first. One step back only; Feature Environments have no Rollback.
- **`destroy feature-<key>`** (starter: on pull request close). Removes the Environment with its volumes and frees its Slot.
- **`sweep --keep <keys>`** or **`--keep-none`** (starter: `deadlift-sweep`, every six hours and by hand). **The Sweep** destroys every Feature Environment whose key isn't kept; `deliver`'s `swept` output lists them. An empty keep list is refused, so a failed API call never sweeps everything.
- **`watchdog`**, run every 5 minutes by the Server's own timer, never by a delivery. Over the upper disk threshold it stops every Feature Environment on the Server, keeping their data and Slots. Dev Environments keep running.

deadlift judges an Environment only by its routed service's healthcheck and the exit codes of the services it depends on (ADR 0002): what happens inside the containers, migrations included, is the App's.
