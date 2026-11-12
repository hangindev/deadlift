---
status: proposed
---

# deadlift knows nothing about what runs inside an App's containers

Eleven's delivery knew a lot about Eleven: a Postgres it dumped and restored, a schema check with three answers, a migrate service, a `/health` probe, and a Component Gallery published beside the app. We decided that deadlift knows none of it. An App is one Compose file holding everything it runs. deadlift needs only `x-deadlift.route: <service>:<port>`, the one service it routes, which must have a Compose `healthcheck`. deadlift starts the App with `docker compose up --wait` and judges it by those healthchecks. We chose this so that deadlift's contract stays small enough for any App to meet, and so that whatever an App adds to its own containers never needs a change in deadlift.

What that means for each thing Eleven had:

- **Migrations** are the App's, ordered by its own `depends_on`. The App's one obligation is that its migration fails, changing nothing, when the data doesn't fit the code. That failure is what tells deadlift a deploy or Rollback can't go ahead.
- **Data** is protected by Volume Snapshots: every named volume of the Dev Environment, copied with its containers stopped. These replace database dumps, so Recovery and Rollback work for any store.
- **Several things to reach**, such as an app and its gallery, go behind one routed service the App runs, for example a proxy.

## Considered Options

- **Postgres built in**, with dumps, restores and a schema check. Rejected: it serves one kind of App, and a dump needs credentials and tools that deadlift would have to know about.
- **Hooks**, such as a `check` run before anything stops. Rejected: the App's own migration answers the same question. The cost is a few seconds of downtime and a red run when data doesn't fit, which is rare and only on the App's own Environments.
- **Sites**, static files published beside an Environment with their own lifecycle. Rejected: a second kind of delivery and a second tar. An App serves its static files from its own Compose file instead, and a PR's static files then live and die with its Feature Environment.
- **Several routed services per Environment.** Rejected until an App needs it. A proxy in the App's Compose file does the same.

## Consequences

- Volume Snapshots stop the whole Dev Environment, the database included, so every Dev deploy has a few seconds of downtime, and the snapshots are the size of the raw volumes.
- Eleven changes to fit: its migration runs its schema check first, and its Component Gallery moves into its Compose file, behind a proxy or its web server.
