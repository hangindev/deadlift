# deadlift

Delivers Apps from GitHub to Servers on a tailnet, as Environments.

## Language

**App**:
A codebase that deadlift delivers, described by one Compose file.
_Avoid_: Project (a Docker Compose project is one Environment), service, tenant

**Server**:
A machine that hosts the Environments of one or more Apps.
_Avoid_: Project Server, host, box, VPS

**Environment**:
One running copy of an App on a Server, at its own address.
_Avoid_: Stage, instance, deployment (which is the act of deploying)

**Dev Environment**:
An App's long-lived Environment, which tracks one branch, `main` by default.
_Avoid_: Prod, staging, Main Environment

**Feature Environment**:
An App's short-lived Environment for one open pull request.
_Avoid_: Preview, review app, branch deploy

**Slot**:
One of an App's limited places on a Server for a Feature Environment.
_Avoid_: Quota, lease

**Sweep**:
Destroying every Feature Environment of an App that isn't in a given list to keep.
_Avoid_: Reconcile, garbage collection, prune (which is only for images)

**Volume Snapshot**:
A copy of a Dev Environment's named volumes, taken before each deploy.
_Avoid_: Backup, dump, Data Snapshot

**Recovery**:
Putting back a Dev Environment's previous version and Volume Snapshot when a deploy fails partway.
_Avoid_: Auto-rollback, revert

**Rollback**:
Going back one version on a Dev Environment, on request.
_Avoid_: Revert (which is a commit), undo, Recovery
