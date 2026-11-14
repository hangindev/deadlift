# The Compose contract

What an App's `compose.yaml` must hold, and may hold, for deadlift to deploy it. The Server checks every deploy against these rules before anything stops, and refuses (exit 2) a template that breaks one, with a `deadlift: ` reason naming the key, such as `services.web.ports`.

## What it must hold

- **One file, everything the App runs.** `compose.yaml` at the repo root, which the starter workflows send (`deliver`'s `compose` input changes the path).
- **One routed service**, named at the top level:

  ```yaml
  x-deadlift:
    route: web:3000 # <service>:<port it listens on inside its container>
  ```

  Every Environment is reached through that service alone. An App with several things to reach puts a proxy of its own in front of them and routes to the proxy.
- **A healthcheck on the routed service, in the template itself**, not only in its image, and not disabled (`disable: true` or `test: ["NONE"]` are refused). deadlift starts the App with `docker compose up` and waits for the routed service to be healthy; healthy is whatever this check says. Give a slow starter `start_period` and `start_interval`.
- **Images only, built per commit by CI.** Each image the App builds is named in the workflows (`web` by default) and reaches the template as `${DEADLIFT_IMAGE_<NAME>}`, the name upper-cased: `web` is `${DEADLIFT_IMAGE_WEB}`, `my_worker` is `${DEADLIFT_IMAGE_MY_WORKER}`. Names are lowercase letters and digits, starting with a letter, joined by single `_`. Public images, such as `postgres:17`, are referenced as usual.

## Migrations and data

- **Migrations run as their own one-shot service**, which the routed service waits for:

  ```yaml
  depends_on:
    migrate:
      condition: service_completed_successfully
  ```

  They run on every deploy. A migration must **fail, changing nothing, when the data is ahead of the code**: that failure is how Recovery and Rollback know an older version can't run on newer data. Many migration tools succeed silently there, so the App checks for it itself.
- **Data lives in named volumes.** Before each Dev deploy deadlift takes a Volume Snapshot of every named volume, with the containers stopped, and Recovery and Rollback put them back. Data kept outside the Server, such as a hosted database, is the App's to protect.
- `tmpfs`, `shm_size` and inline `configs` (`content:`) are fine.

## Variables

Compose's `${VAR}` substitution is the only way values reach the template; pass them into containers with `environment:`. Nothing of the Server's or CI's environment is visible. The values come from:

- the App's secrets files on the Server, one for its Dev Environment and one for all its Feature Environments, put there by `deadlift app secrets`;
- deadlift, which sets, and wins over the secrets files:
  - `DEADLIFT_ENV`: `dev` or `feature-<key>`;
  - `DEADLIFT_APP`: the App's name;
  - `DEADLIFT_URL`: the Environment's address, such as `https://shop.tail1234.ts.net/`;
  - `DEADLIFT_SECRET`: random, made on the Environment's first deploy and kept until it's destroyed, to derive passwords from (a database password the App never has to manage);
  - `DEADLIFT_IMAGE_<NAME>`: each built image.

## What is refused

The template is normalised with `docker compose config`, then every key is checked against an allowlist: a key not on it is refused, even one Compose adds later. The ones Apps usually reach for, each refused because it reaches past the App's own Compose project to the Server or another App:

- Host files: bind mounts (`./data:/data`, `/etc/x:/x`), the Docker socket, `env_file`, `label_file`, `include`, `extends`, and `configs` or `secrets` with `file:`.
- `ports`: deadlift publishes the routed service itself, on the Server's loopback, and nothing else.
- `build`, and `pull_policy: build`: CI builds and pushes the images.
- `privileged`, `cap_add`, `devices`, `gpus`, `security_opt` (except `no-new-privileges`), `sysctls`, `runtime`, `cgroup_parent`, `userns_mode`, `uts`.
- `network_mode`, `pid`, `ipc` or `cgroup` set to `host` or `container:…`; `volumes_from`.
- `external` networks or volumes, a custom `name:` on a network or volume, a driver other than the default, `driver_opts`, and `ipam` settings.
- `container_name` (shared by every App on the Server), `external_links`.
- `deploy`, `oom_score_adj`, `oom_kill_disable`: limits come from the App's settings (`deadlift app add --memory --cpus --pids`), and a lower limit in the template is kept.
- Labels starting `deadlift.` or `com.docker.`.

Allowed on a service, as they are: `image`, `command`, `entrypoint`, `environment`, `depends_on`, `healthcheck`, `volumes` (named or tmpfs), `tmpfs`, `configs`, `secrets`, `networks` (with `aliases`), `expose`, `labels`, `restart`, `user`, `working_dir`, `read_only`, `init`, `stop_signal`, `stop_grace_period`, `shm_size`, `mem_limit`, `cpus`, `pids_limit`, `cap_drop`, `profiles`, `platform`, `hostname`, `domainname`, `dns`, `dns_opt`, `dns_search`, `extra_hosts`, `group_add`, `links`, `scale`, `stdin_open`, `tty`, `attach`.

## What a container can reach

The internet, and the other services of its own Environment. Not the Server, not the tailnet, not the cloud metadata address, not private ranges, and not another App's or another Environment's containers. An App that needs a service on the tailnet reaches it some other way, such as over the internet.
