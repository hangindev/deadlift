---
name: setup-deadlift
description: Sets an App up on deadlift, from nothing to its first Dev and Feature deploys: the Compose contract, `deadlift app add`, the tailnet policy, GitHub OIDC, secrets and `deadlift init`. Use when adding an App to a deadlift Server, or when merging the tailnet policy entries a deadlift wizard printed.
---

# Set up an App on deadlift

deadlift delivers an App, described by one `compose.yaml`, from GitHub to a Server on a tailnet. This skill takes an App from nothing to two working Environments: its Dev Environment at `https://<app>.<tailnet>.ts.net/`, and a pull request's Feature Environment at a port on that name. Operating it from then on, and diagnosing a failed or skipped delivery, is the `ask-deadlift` skill's.

## Who does what

- **The `deadlift` CLI** runs on the maintainer's computer and is deterministic: it does its part and prints what it can't do, such as the tailnet policy entries it needs.
- **You** run the CLI, edit the App's repo, and merge the tailnet policy into a validated proposal with its diff.
- **The maintainer** does what needs their accounts and judgement: saving the tailnet policy, the Tailscale admin console, and secret values.

The maintainer saves the tailnet policy, always: you hand them the merged policy and its diff, and never write it through the API or the console yourself.

## Before you start

Find out, asking the maintainer for what you can't find:

- **The App's name**: up to 23 of `a-z`, `0-9` and `-`, starting with a letter, not ending with `-`, and with no part named `feature` after a `-`. It names the deploy user `deadlift-<app>`, the CI tag `tag:deadlift-ci-<app>`, the Tailscale Service `svc:<app>` and the address.
- **The Server**, by its tailnet name, such as `deadlift-1`. With no Server yet, the maintainer runs `deadlift server provision` first: a wizard that only they can do, since it buys a machine. When it stops at the tailnet policy, merge its entries with [step 3](#3-merge-the-tailnet-policy), then let them carry on.
- **The App's GitHub repo**, `<owner>/<name>`, where its workflows run.
- **The `deadlift` command**: `bin/deadlift` in a checkout of `hangindev/deadlift`, on Python 3.12 or later.
- **A Tailscale API access token** in `TAILSCALE_API_KEY` (admin console, Settings, Keys), and `TAILSCALE_TAILNET` when it isn't the token's own tailnet. `deadlift app add` uses it, and so do you, to read and validate the policy. Keep it out of files, commands you echo, and anything you show.
- SSH as root to the Server (`ssh root@<server> true`), and `gh` logged in with access to the repo.

Work in a scratch directory outside the App's repo for the files below: they hold the policy and secrets, and none is committed.

## 1. Meet the Compose contract

Read [COMPOSE-CONTRACT.md](COMPOSE-CONTRACT.md), then write the App's `compose.yaml`, or change the one it has, until it keeps every rule there. Note each image the App builds and its name: `deadlift init` needs them in step 6.

Done when every rule holds and, where Docker is available, `docker compose config --quiet` passes with each `${VAR}` the template reads set to a stand-in.

## 2. Give the App its place on the Server

```sh
deadlift app add <app> --server <server> --repo <owner>/<name> [--slots <1-9>] [--snapshots <n>] [--memory <size>] [--cpus <n>] [--pids <n>]
```

Ask the maintainer how many Feature Environments the App may have at once (`--slots`, 3 by default) and whether its containers need more than the default limits (each one `1g` of memory, 1 CPU and 512 processes). It creates the deploy user, its one sudo rule, the App's block of ports and the Service `svc:<app>`, then prints the App's own tailnet policy entries, a JSON object, and the federated credential its CI needs. Running it again is safe, and changes only the settings given.

Save the JSON object exactly as printed to `app-entries.hujson`, and keep the federated credential settings for step 4.

Done when it exits 0. On any other exit, its `deadlift: ` reason line says what's wrong and what to run; act on it and run `app add` again.

## 3. Merge the tailnet policy

The policy decides who reaches the Server. deadlift's entries let the App's CI, as `tag:deadlift-ci-<app>`, reach only the Server's SSH port and log in only as `deadlift-<app>` (ADR 0001 in `hangindev/deadlift`).

1. **Get the policy as it is now**, as HuJSON with its comments, into `policy.hujson`: pasted by the maintainer, or read with their token:

   ```sh
   curl -sS --fail-with-body -H "Authorization: Bearer $TAILSCALE_API_KEY" -H "Accept: application/hujson" \
     "https://api.tailscale.com/api/v2/tailnet/${TAILSCALE_TAILNET:--}/acl" -o policy.hujson
   ```

   A policy kept in a repo through GitOps is that repo's file: work on a copy the same way, and the maintainer commits it.
2. **Gather the entries to merge**, one HuJSON object per file:
   - `app-entries.hujson`, from step 2.
   - The provision wizard's entries, when the policy doesn't have them yet (no owner for `tag:deadlift-server`, or no `autoApprovers.services` entry for `tag:deadlift-svc`): the file `cli/tailscale-policy.hujson` in the deadlift checkout, as it is.
   - `tests-entries.hujson`, the policy tests, which Tailscale runs on every save from then on, so no later edit can widen the CI's reach unnoticed:

     ```hujson
     {
     	"tests": [
     		// <app>'s CI reaches the Server's SSH port, and nothing else on it.
     		{
     			"src":    "tag:deadlift-ci-<app>",
     			"accept": ["tag:deadlift-server:22"],
     			"deny":   ["tag:deadlift-server:443"],
     		},
     	],
     	"sshTests": [
     		// <app>'s CI logs in to the Server only as <app>'s deploy user.
     		{
     			"src":    "tag:deadlift-ci-<app>",
     			"dst":    ["tag:deadlift-server"],
     			"accept": ["deadlift-<app>"],
     			"deny":   ["root", "deadlift-<other>"],
     		},
     	],
     }
     ```

     `deny` names `root` and the deploy user of every other App in the policy: one `deadlift-<other>` for each `tag:deadlift-ci-<other>` in its `tagOwners`. With no other App, it's `["root"]`.
3. **Merge them**, with the script beside this file:

   ```sh
   python3 <this skill's directory>/scripts/merge_policy.py policy.hujson app-entries.hujson tests-entries.hujson > merged.hujson
   ```

   It adds each entry the policy lacks after the last of its section, adds nothing the policy has already, keeps every other byte, and lists each addition as a `deadlift: added …` line. Exit 2 is a refusal, with its reason: a file that isn't HuJSON (fix the paste), or an entry that contradicts the policy, which the maintainer settles before you merge that one by hand.
4. **Validate the merged policy**, which also runs its tests. This saves nothing:

   ```sh
   curl -sS -w '\nHTTP %{http_code}\n' -H "Authorization: Bearer $TAILSCALE_API_KEY" -H "Content-Type: application/hujson" \
     --data-binary @merged.hujson "https://api.tailscale.com/api/v2/tailnet/${TAILSCALE_TAILNET:--}/acl/validate"
   ```

   It passes on HTTP 200 with an empty body or `{}`. Anything with a `message` hasn't passed: `"test(s) failed"` with each failing test in `data[].errors`, a syntax error, or `"warning(s) found"`.

   A failing test means a rule in the policy gives the CI tag more than deadlift's entries do. Keep the tests as they are, find that rule, and show the maintainer it with a narrowing. The usual one is a tailnet's default grant `{"src": ["*"], "dst": ["*"], "ip": ["*"]}`, which lets every device, tagged ones included, reach everything: its `src` narrowed to `["autogroup:member"]` keeps every person's access and leaves tagged devices to their own grants. The maintainer decides; with their yes, make that edit in `merged.hujson` and validate again.
5. **Hand it over.** Show the diff, `diff -u policy.hujson merged.hujson`, list what it adds, and give the maintainer `merged.hujson` to save: pasted whole into the admin console's Access controls editor, or committed through GitOps. Someone may have saved the policy since you read it, so a console save that warns of that means starting this step over from 1.

Done when the maintainer has saved it, and the policy read again (sub-step 1) through `merge_policy.py` with the same entries says `nothing was added`.

## 4. Give the App's CI its federated credential

The App's CI joins the tailnet through GitHub's OIDC token, with no key to leak. In the admin console, Settings, Trust credentials, the maintainer adds the OpenID Connect credential `app add` printed: issuer GitHub, subject `repo:<owner>/<name>:*`, scope Auth Keys write with the tag `tag:deadlift-ci-<app>`. The tag must be in the saved policy first, so this follows step 3. If `gh api repos/<owner>/<name>/actions/oidc/customization/sub` shows a custom subject claim, the subject takes that form instead.

They give you the credential's Client ID and Audience, which you set as the repo's variables:

```sh
gh variable set TS_OAUTH_CLIENT_ID --repo <owner>/<name> --body <client-id>
gh variable set TS_AUDIENCE --repo <owner>/<name> --body <audience>
```

Done when `gh variable list --repo <owner>/<name>` shows both.

## 5. Put the App's secrets on the Server

Every `${VAR}` the template reads, other than the `DEADLIFT_*` ones, comes from the App's secrets files: one for its Dev Environment, one for all its Feature Environments. List those variables for the maintainer; they write the values into env files (`NAME=value`, one per line) in the scratch directory, and you send them:

```sh
deadlift app secrets <app> dev dev.env --server <server>
deadlift app secrets <app> feature feature.env --server <server>
```

Secrets never pass through CI or the repo. A new file takes effect from the next deploy of its kind; the local copies can go once sent.

Done when every variable the template reads without a default has a value in each file.

## 6. Write the workflows

In the App's repo:

```sh
deadlift init <app> --server <server> [--branch <branch>] [--image <name>]...
```

It writes `deadlift-feature`, `deadlift-dev`, `deadlift-sweep` and `deadlift-rollback` into `.github/workflows/`, building each `--image` (`web` by default) from the repo root's `Dockerfile`, with Dev tracking `--branch` (`main` by default). An image built from elsewhere gets `context=`, `file=` or `target=` on its line in the `images:` block of `deadlift-feature` and `deadlift-dev`. The workflows are the App's own from then on, to change as it likes.

A pull request labelled `skip-deploy` gets no Feature Environment: `gh label create skip-deploy --repo <owner>/<name>` makes the label available.

Done when the four workflows exist and their `images:` blocks build every `${DEADLIFT_IMAGE_<NAME>}` the template reads.

## 7. Deliver for the first time

1. Commit `compose.yaml`, the Dockerfiles and the workflows on a branch, and open a pull request. `deadlift-feature` builds and deploys `feature-<number>`, and comments the address on the pull request. Done when that address answers from a device on the tailnet (`curl -sSf <url>` from the maintainer's computer).
2. Merge it. `deadlift-dev` deploys the Dev Environment. Done when `https://<app>.<tailnet>.ts.net/` answers.

A Service its Server has just started serving can take some seconds to answer. A delivery that failed or was skipped says why in its job summary, on a line starting `deadlift: `; the `ask-deadlift` skill reads those.

The App is set up when both addresses answer.
