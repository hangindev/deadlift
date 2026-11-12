---
status: proposed
---

# deadlift knows nothing about what runs inside an App's containers

A deploy system can know a lot about what it deploys: which database to dump and restore, a schema check to run first, a migration step, a health endpoint to probe, static files to publish alongside. Each one makes deploys a little smarter for Apps that fit it and a little harder for Apps that don't. We decided that deadlift knows none of it. An App is one Compose file holding everything it runs. deadlift needs only `x-deadlift.route: <service>:<port>`, the one service it routes, which must have a Compose `healthcheck`. deadlift starts the App with `docker compose up --wait` and judges it by those healthchecks. We chose this so that deadlift's contract stays small enough for any App to meet, and so that whatever an App adds to its own containers never needs a change in deadlift.

What that means for the things a deploy system would usually know:

- **Migrations** are the App's, ordered by its own `depends_on`. The App's one obligation is that its migration fails, changing nothing, when the data doesn't fit the code. That failure is what tells deadlift a deploy or Rollback can't go ahead.
- **Data** is protected by Volume Snapshots: every named volume of the Dev Environment, copied with its containers stopped. They work the same whatever the volumes hold, so Recovery and Rollback work for any store.
- **Several things to reach**, such as an app and its static files, go behind one routed service the App runs, for example a proxy.

## Considered Options

- **A built-in database**, such as Postgres with dumps, restores and a schema check. Rejected: it serves one kind of App, and a dump needs credentials and tools that deadlift would have to know about.
- **Hooks**, such as a check run before anything stops. Rejected: the App's own migration answers the same question. The cost is a few seconds of downtime and a red run when data doesn't fit, which is rare and only on the App's own Environments.
- **Static sites published beside an Environment**, with their own lifecycle. Rejected: a second kind of delivery. An App serves its static files from its own Compose file instead, and they then live and die with its Environment.
- **Several routed services per Environment.** Rejected until an App needs it. A proxy in the App's Compose file does the same.

## Consequences

- Volume Snapshots stop the whole Dev Environment, a database included, so every Dev deploy has a few seconds of downtime, and the snapshots are the size of the raw volumes.
- Data an App keeps outside the Server, such as a hosted database, isn't in any Volume Snapshot. Recovery and Rollback put back only the version for it, and the App protects that data itself.
- An App whose migration tool succeeds without changing anything when the database is ahead of the code, as many do, must check for that itself and fail.
