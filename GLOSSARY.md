# deadlift

Delivers self-hosted apps from GitHub onto a Server reached only over a tailnet: a long-lived Environment that tracks `main`, and a short-lived Environment for each pull request.

## Language

**App**:
A repository whose code deadlift delivers to a Server, described by one Compose file that holds everything it runs. Several Apps may share one Server.
_Avoid_: Project (a Docker Compose project is one Environment), service, tenant

**Default App**:
The one App on a Server whose Dev Environment takes the Server's plain HTTPS address, on port 443.
_Avoid_: Primary app, main app

**Server**:
A dedicated machine that deadlift sets up and delivers to, hosting the Environments of one or more Apps.
_Avoid_: Project Server, host, box, VPS

**Environment**:
A running deployment of an App, with its own data and its own address.
_Avoid_: Stage, instance, deployment

**Dev Environment**:
An App's long-lived Environment, which tracks `main`. There is no production Environment: production runs elsewhere.
_Avoid_: Prod, staging, Main Environment

**Feature Environment**:
The short-lived Environment for one open pull request.
_Avoid_: Preview, review app, branch deploy

**Recovery**:
What a Dev Environment's deploy does by itself when the new version fails after the Environment has stopped: it puts back the data and version that ran before.
_Avoid_: Auto-rollback, revert

**Rollback**:
Going back one version on a Dev Environment, by hand, after a deploy that succeeded. It keeps the current data unless told to discard it.
_Avoid_: Revert (which is a commit on `main`), undo, Recovery

**Volume Snapshot**:
A copy of every named volume of a Dev Environment, taken with its containers stopped before each deploy, that Recovery and Rollback restore from.
_Avoid_: Backup, dump, Data Snapshot

**Sweep**:
The scheduled cleanup that destroys Feature Environments whose pull request is no longer open, catching what the cleanup on close missed. Destroying them against the list of open pull requests is a reconcile, which a deploy that finds no free Slot also runs.
_Avoid_: Garbage collection, prune (which is only for images)

**Slot**:
One of an App's limited places for a Feature Environment on its Server. An Environment holds its Slot until it's destroyed. A pull request that finds none free is skipped, not queued.
_Avoid_: Quota, lease
