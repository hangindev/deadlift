# deadlift's reason lines, diagnosed

Each entry quotes reason lines as deadlift prints them, with `<placeholders>` for what varies and `…` where the line goes on with the same. Exit codes are the Server's: 0 done, 1 failed, 2 refused, 75 skipped.

## Contents

- No free Slot
- The disk thresholds
- A refused template
- An unhealthy routed service
- A failed migration, and Recovery
- A refused Rollback
- An out-of-date Server: re-run bootstrap
- Reaching the Server
- The Sweep and destroy
- A Dev deploy that never reached the Server

## No free Slot

``deadlift: all <n> of <app>'s Slots are held, by <environments>, so feature-<key> wasn't deployed. Destroy one of them to free its Slot`` (75, skipped)

Every Slot is held by another Feature Environment, so this one wasn't created; nothing else changed. In the starter workflows `--keep` already frees Slots held by closed pull requests, so the holders are open pull requests, including ones labelled `skip-deploy`. Fixes, in order:

1. Close or finish a pull request that holds a Slot: closing destroys its Environment. Then push again, or re-run the skipped job.
2. If a holder's pull request is already closed, its destroy failed: run `deadlift-sweep` by hand.
3. If the App needs more at once, the maintainer raises its Slots (1 to 9): `deadlift app add <app> --server <server> --repo <owner/name> --slots <n>`.

## The disk thresholds

Two Server-wide thresholds, a percentage of the disk used, are shown by `deadlift server status`: Feature deploys are skipped over the lower one (80% unless the maintainer changed it), and the watchdog stops Feature Environments over the upper one (90%).

``deadlift: the disk is <used>% full, over the Server's <limit>% limit for Feature deploys, so feature-<key> wasn't deployed, and it was left as it was. Dev deploys still go ahead`` (75, skipped)

The Feature Environment was left as it was, running its previous version if it had one.

``deadlift: the disk is <used>% full, over the Server's <limit>% limit, so the watchdog stopped every Feature Environment on the Server. Each starts again on its next deploy, and every Dev Environment is still running``

The watchdog runs on the Server, not in CI, so this line is in its log (`journalctl -u deadlift-watchdog.service`, as root). The sign in CI is a Feature Environment that stopped answering, and `deadlift server status` showing its containers not running. Each starts again on its next deploy, once the disk is below the lower threshold.

For both, free disk, then deploy again. Every App on the Server shares the disk, so the space may be another App's. The maintainer can:

- close pull requests, or run `deadlift-sweep`, to destroy Feature Environments and their volumes;
- keep fewer Volume Snapshots of Dev: `deadlift app add <app> --server <server> --repo <owner/name> --snapshots <n>`, which takes effect on the next Dev deploy.

Unused images of the App are already removed after every deploy. A line saying the disk couldn't be measured (1, failed) is a fault on the Server for the maintainer.

## A refused template

Every template refusal ends `Nothing was changed` and exits 2: it's checked before anything stops, so Dev is still up on its current version and a Feature Environment is as it was. Fix `compose.yaml` in the App's repo and push. The common ones:

- ``deadlift: the template has no x-deadlift.route. Name the service deadlift routes to as `x-deadlift.route: <service>:<port>`. Nothing was changed``
- ``deadlift: x-deadlift.route is '<route>', and it must be <service>:<port>. Nothing was changed``
- ``deadlift: x-deadlift.route names the service <service>, and the template has none. Nothing was changed``
- ``deadlift: the routed service <service> has no healthcheck in the template, and deadlift judges an Environment by it (ADR 0002). Nothing was changed``: a `HEALTHCHECK` in the image doesn't count; give the service a Compose `healthcheck:`.
- ``deadlift: the routed service <service> has its healthcheck disabled, and deadlift judges an Environment by it (ADR 0002). Nothing was changed``
- ``deadlift: the template's <path> <why>, and ADR 0001 refuses it. Nothing was changed``: a key that would let the App reach past its own containers. For example ``deadlift: the template's services.<service>.ports is set: only deadlift publishes, and only the routed service, on the Server's loopback, and ADR 0001 refuses it. Nothing was changed``: drop `ports:`, as deadlift publishes the routed service itself. A bind mount, `privileged`, `network_mode: host`, `build` and the Docker socket are refused the same way; use named volumes, and images built by CI. There's no exception to ask for: ADR 0001 keeps the Apps on a Server apart.
- ``deadlift: the template uses <key>, which would read a file on the Server, and ADR 0001 refuses it. Nothing was changed``: `include`, `extends`, `env_file`, `label_file`, or a `configs` or `secrets` entry with `file:`. Put variables in the App's secrets files (`deadlift app secrets`, run by the maintainer) and use `${VAR}`; inline `configs` with `content:` are allowed.
- ``deadlift: the template isn't valid YAML: …`` and ``deadlift: the template isn't valid Compose: <compose's error>. Nothing was changed``: run `docker compose config` on it locally.

A template's `${VAR}` sees only the App's secrets file for that kind of Environment and deadlift's own variables (`DEADLIFT_ENV`, `DEADLIFT_APP`, `DEADLIFT_URL`, `DEADLIFT_SECRET`, `DEADLIFT_IMAGE_<NAME>`). A broken secrets file fails (1) before anything stops:

``deadlift: <app>'s <file> on this Server isn't a valid env file: <error>. Fix it and re-run `deadlift app secrets <app> <kind>`. Nothing was changed``

## An unhealthy routed service

The routed service's Compose healthcheck reported unhealthy, or hadn't passed after 5 minutes, so deadlift counts the Environment as not started. The quoted cause is Compose's, such as `container <container> is unhealthy`.

- Feature: ``deadlift: feature-<key> didn't start: <cause>. It's stopped, and the App's other Environments are as they were`` (1). Its containers are stopped but kept, so the maintainer can read them on the Server as root: `docker logs deadlift-<app>-feature-<key>-<service>-1`.
- Dev: ``deadlift: dev didn't start on its new version: <cause>. Recovery …``, then Recovery, below.

The fault is in the App: the new version never answers its own healthcheck. Reproduce it with the same image and `docker compose up --wait` locally, fix it and push. The variables differ on the Server, so check the ones it reads (`DEADLIFT_URL`, the secrets file's).

## A failed migration, and Recovery

A migration is the App's own one-shot service that the routed service `depends_on`. When it exits non-zero, Compose's line is `service "<service>" didn't complete successfully: exit <n>` and the routed service never starts. That's what a migration should do when the data doesn't fit the code (ADR 0002). Its own output is in the lines above the reason.

On a Feature Environment: ``deadlift: feature-<key> didn't start: <cause>. It's stopped, …`` (1), with Compose's line as the cause. A Feature Environment keeps its data across redeploys, so its data can be ahead of the code, as when a later push undoes a migration an earlier push ran. Fix the migration, or have the maintainer destroy the Environment (closing the pull request does), which deletes its data, then deploy it fresh.

On Dev, every failure after Dev stopped runs **Recovery**: it brings Dev down, puts back the Volume Snapshot taken just before this deploy, and starts the previous version. The reason says how that went (all 1, failed):

- ``deadlift: dev didn't start on its new version: <cause>. Recovery succeeded: Dev is back on its previous version, with its data as it was before this deploy, at <url>``: Dev is up and unharmed. Fix the App and push again.
- ``deadlift: dev didn't start on its new version: <cause>. Recovery put back Dev's data as it was before this deploy, and there was no previous version to start, so Dev is stopped``: the first deploy of Dev failed. Fix and push; there's nothing to lose.
- ``deadlift: dev didn't start on its new version: <cause>. Recovery failed: it put back Dev's data as it was before this deploy, but the previous version didn't start either (<cause>), so Dev is stopped``: Dev is down. Tell the maintainer now. The data is back as it was; the second cause is why the old version failed, such as its image gone from the Server.
- ``deadlift: dev didn't start on its new version: <cause>. Recovery failed: it couldn't put back the Volume Snapshot taken before this deploy, <snapshot>, so Dev is stopped``: Dev is down and its data is as the failed version left it, with the snapshot still on the Server. The maintainer's, urgently.

Failures before Dev stopped change nothing (1): ``deadlift: couldn't pull <ref>: <docker's error>. Nothing was changed`` (see Reaching the Server). A failure while stopping Dev or taking its snapshot says where Dev stands, such as ``deadlift: couldn't take a Volume Snapshot of dev, so its new version wasn't started. Dev is up on its current version, at <url>``.

## A refused Rollback

Rollback goes back one version on Dev only. Its refusals (2) leave Dev on its current version:

- ``deadlift: dev's previous version didn't start on today's data: <cause>. Rollback was refused, and Dev is up on its current version again, with its data as it was, at <url>. To roll back to the data from before the current version was deployed, discarding what's been written since, run it again with --discard-newer-data, which keeps a Volume Snapshot of today's data first``: usually the previous version's migration refuses data the current version migrated. Running it again with `discard-newer-data` ticked loses everything written to Dev since the current version was deployed (kept only in a Volume Snapshot on the Server). Ask the maintainer before you do. The alternative is a fix forward: deploy a commit that works with today's data.
- ``deadlift: dev has no previous version to roll back to: Rollback goes back one step only, and only once Dev has had two versions. To go further back, deploy an earlier commit. Nothing was changed``: Dev was rolled back already, or has had one version. To go back further, revert on the tracked branch and let `deadlift-dev` deploy it.
- ``deadlift: dev hasn't been deployed, so there's nothing to roll back``
- ``deadlift: Rollback is only for the Dev Environment, dev, and feature-<key> is a Feature Environment: deploy it again from an earlier commit instead. Nothing was changed``

With `--discard-newer-data`:

- ``deadlift: rolled dev back to its previous version, on its data from before the current version, which is up at <url>. Today's data is kept in the Volume Snapshot <snapshot>`` (0): done; name the snapshot to the maintainer, the one place today's data still exists.
- ``deadlift: the Volume Snapshot from before dev's current version was deployed is gone, so --discard-newer-data can't go back to it. Nothing was changed`` (1): that snapshot isn't on the Server any more, so there's nothing to go back to. Fix forward, or ask the maintainer.
- ``deadlift: dev's previous version didn't start on today's data, and <why>. Nothing was rolled back: Dev is up on its current version again, with today's data, at <url>`` (1): the previous version doesn't start on the old data either. Fix forward.

A plain Rollback that worked says ``deadlift: rolled dev back to its previous version, on today's data, which is up at <url>``.

## An out-of-date Server: re-run bootstrap

deadlift's code on a Server is installed by `deadlift server bootstrap` and never delivered (ADR 0001), so a Server keeps the version it was last bootstrapped with, while the App's workflows use `deliver@v1`, which moves.

- ``deadlift: refused the delivery: it uses delivery protocol '<n>', and this Server speaks only <versions>. Re-run `deadlift server bootstrap` to bring the Server up to date. Nothing was run.`` (2)
- From the maintainer's `deadlift` command: ``deadlift: <server> has no /usr/local/lib/deadlift/app. Run `deadlift server bootstrap <server>` to bring it up to date, then run this again.``

The fix is the maintainer's: `deadlift server bootstrap <server>`. It's safe to re-run, and restarts nothing that doesn't need it. Then re-run the failed job.

## Reaching the Server

- ``deadlift: couldn't reach <server> over SSH as deadlift-<app>, so nothing was run.`` (1, from `deliver` itself): CI never got in. Check, in order: the `deliver` action's Tailscale step joined the tailnet (repo variables `TS_OAUTH_CLIENT_ID` and `TS_AUDIENCE`, and the job's `id-token: write`); the tailnet policy holds the App's entries that `deadlift app add` printed (its CI tag `tag:deadlift-ci-<app>`, that tag's grant to the Server's SSH port, its SSH rule as `deadlift-<app>`); the App has been added to this Server (`deadlift app add`, which makes the deploy user); the Server is online and named as the workflow's `DEADLIFT_SERVER` says. The setup-deadlift skill covers setting these up.
- ``deadlift: refused the delivery: 'deadlift-<app>' isn't the deploy user of an App on this Server. Nothing was run.`` (2): the deploy user exists but the App's entry on the Server doesn't, as when `deadlift app remove` stopped partway. The maintainer runs `deadlift app add` again.
- ``deadlift: couldn't pull <ref>: <docker's error>. Nothing was changed`` (1): the Server pulls with the job's token, so the job needs `packages: read`, and the image must be in GHCR, where the workflow's `build` job pushes it. Docker's error says which: not found, or denied.
- ``deadlift: couldn't read the tailnet's name from Tailscale, so nothing was changed`` (1), and ``deadlift: <environment> started, but Tailscale Serve couldn't map it at <url>, so it's stopped again`` (1): Tailscale on the Server. The maintainer's.

## The Sweep and destroy

- ``deadlift: sweep needs --keep with the keys of the Feature Environments to keep, or --keep-none to sweep them all. Nothing was swept`` and ``deadlift: --keep lists no key, so nothing was swept or deployed. Give --keep-none to sweep every Feature Environment`` (2): the keep list came out empty. In the starter it's the open pull requests, and `keep-none` is passed only when there are none, so check the step that listed them.
- ``deadlift: couldn't destroy <environments> (above), after sweeping <environments>. Sweep again to finish`` (1): the lines above say why. Run `deadlift-sweep` again; what's left is still listed until it goes.
- ``deadlift: swept <environments>, keeping <environments>``, ``deadlift: swept nothing: …`` and ``deadlift: destroyed <environment> with its volumes, freeing Slot <n>`` (0): done.
- ``deadlift: <environment> isn't on this Server, so there was nothing to destroy`` (0): done, already gone, as when a Sweep or an earlier destroy got there first.

## A Dev deploy that never reached the Server

`deadlift-dev` asks the `superseded` action first, and skips the deploy when the branch has moved on, writing `Not deployed.` and the reason in the job summary:

``deadlift: <branch> has moved past <commit> to <head>, whose own run deploys it.``

Nothing is wrong: a newer run deploys the branch's head. Look at that run instead.
