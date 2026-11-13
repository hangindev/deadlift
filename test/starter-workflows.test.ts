// The starter workflows, as `deadlift init` writes them into an App's repository, run for GitHub's
// events on a fake runner (support/workflow.ts) against a fake GitHub. deadlift's own actions are
// stood in for: their tests (deliver-action, build-action, superseded, prune-registry) check what
// they do with their inputs, and these check what each workflow asks of them, and what it does
// with what they answer: the pull request's comment and the commit's check.
import { afterEach, describe, expect, it } from "vitest";
import type { Value } from "./support/expressions.ts";
import { appRepository } from "./support/app-repository.ts";
import { fakeGitHub, type GitHubState, type PullRequest } from "./support/fake-github.ts";
import { runWorkflow, type UsesStub, type WorkflowEvent } from "./support/workflow.ts";

const cleanups: (() => void)[] = [];
afterEach(() => cleanups.splice(0).forEach((cleanup) => cleanup()));

const repo = "acme/shop";
const sha = (c: string) => c.repeat(40);

function pull(number: number, head: string, extra: Partial<PullRequest> = {}): PullRequest {
  return { number, state: "open", head: { sha: head, repo: { full_name: repo } }, labels: [], ...extra };
}

/** What a stand-in deliver answers: its outputs, and a failing step for `failed`. */
interface Answer {
  outcome: "done" | "skipped" | "failed";
  reason: string;
  url?: string;
  environment?: string;
  swept?: string;
}

/**
 * An App's repository after `deadlift init`, with a fake GitHub holding acme/shop and stand-ins
 * for the actions its workflows use. `deliver` answers with `answer`, and the stand-ins record
 * what they were asked.
 */
function app(state: Partial<NonNullable<GitHubState["repos"]>[string]> = {}, initArgs: string[] = []) {
  const repository = appRepository();
  cleanups.push(repository.cleanup);
  const init = repository.init(["shop", "--server", "deadlift-1", ...initArgs]);
  if (init.status !== 0) throw new Error(init.stderr);
  const github = fakeGitHub({
    repos: { [repo]: { ownerType: "Organization", branches: {}, commits: {}, ...state } },
  });
  cleanups.push(github.cleanup);

  let answer: Answer = { outcome: "done", reason: "deadlift: done" };
  let superseded = { superseded: "false", head: "", reason: "deadlift: not superseded" };
  const stubs: Record<string, UsesStub> = {
    "actions/checkout": () => {},
    "hangindev/deadlift/actions/build": (inputs) => ({
      outputs: {
        images: inputs.images
          .split("\n")
          .filter(Boolean)
          .map((line) => line.split(" ")[0])
          .map((name) => `${name}=ghcr.io/${repo}/${name}:sha-${inputs.commit ?? "push"}`)
          .join("\n"),
      },
    }),
    "hangindev/deadlift/actions/deliver": () => ({
      status: answer.outcome === "failed" ? 1 : 0,
      outputs: { environment: "", url: "", swept: "", ...answer },
    }),
    "hangindev/deadlift/actions/superseded": () => ({ outputs: superseded }),
    "hangindev/deadlift/actions/prune-registry": () => {},
  };

  const run = (workflow: string, event: WorkflowEvent, context: Record<string, Value> = {}) => {
    const result = runWorkflow(repository.workflow(workflow), event, {
      github: { repository: repo, token: "fake-job-token", ...context },
      vars: { TS_OAUTH_CLIENT_ID: "client-123", TS_AUDIENCE: "api.tailscale.com/client-123" },
      uses: stubs,
      env: github.env,
      cwd: repository.dir,
    });
    const asked = (action: string) =>
      result.used.filter((u) => u.action === `hangindev/deadlift/actions/${action}`).map((u) => u.with);
    return { ...result, asked, delivered: asked("deliver") };
  };

  return {
    github,
    run,
    deliverAnswers: (a: Answer) => (answer = a),
    supersededAnswers: (s: typeof superseded) => (superseded = s),
    comments: (pr: number) => (github.state().repos![repo].comments ?? []).filter((c) => c.issue === pr),
    checks: () => github.state().repos![repo].checkRuns ?? [],
    /** A pull_request event for this pull request, as GitHub sends it. */
    pullRequest(action: string, pr: PullRequest, extra: Record<string, Value> = {}) {
      return run("deadlift-feature", {
        name: "pull_request",
        payload: { action, number: pr.number, pull_request: pr as unknown as Value, ...extra },
      });
    },
  };
}

const up = (n: number): Answer => ({
  outcome: "done",
  reason: `deadlift: deployed feature-${n}, which is up at https://shop.tailtest.ts.net:10001/`,
  url: "https://shop.tailtest.ts.net:10001/",
  environment: `feature-${n}`,
});

describe("deadlift-feature", () => {
  it("deploys a pull request's Feature Environment, keyed by its number, keeping every open one's", () => {
    const pr = pull(42, sha("a"));
    const fork = pull(60, sha("f"), { head: { sha: sha("f"), repo: { full_name: "someone/shop" } } });
    const fake = app({ pulls: [pull(57, sha("b")), pr, fork, pull(13, sha("c"), { state: "closed" })] });
    fake.deliverAnswers(up(42));

    const run = fake.pullRequest("opened", pr);

    expect(run.jobs.deliver.result, run.stderr).toBe("success");
    expect(run.asked("build")).toEqual([expect.objectContaining({ commit: sha("a"), images: "web\n" })]);
    expect(run.delivered).toEqual([
      expect.objectContaining({
        app: "shop",
        server: "deadlift-1",
        operation: "deploy",
        environment: "feature-42",
        images: `web=ghcr.io/${repo}/web:sha-${sha("a")}`,
        keep: "57,42",
        "oauth-client-id": "client-123",
        audience: "api.tailscale.com/client-123",
      }),
    ]);
  });
});

describe("deadlift-feature's comment and check", () => {
  it("comments the Environment's address, and sets a successful check on the commit", () => {
    const pr = pull(42, sha("a"));
    const fake = app({ pulls: [pr] });
    fake.deliverAnswers(up(42));

    fake.pullRequest("opened", pr);

    const comments = fake.comments(42);
    expect(comments).toHaveLength(1);
    expect(comments[0].body).toContain("https://shop.tailtest.ts.net:10001/");
    expect(comments[0].body).toContain("`aaaaaaa`");
    expect(fake.checks()).toEqual([
      expect.objectContaining({
        name: "Feature Environment",
        head_sha: sha("a"),
        status: "completed",
        conclusion: "success",
      }),
    ]);
  });

  it("keeps one comment, edited in place, as the pull request moves on", () => {
    const fake = app({ pulls: [pull(42, sha("a"))] });
    fake.deliverAnswers(up(42));
    fake.pullRequest("opened", pull(42, sha("a")));
    fake.github.update((s) => (s.repos![repo].pulls = [pull(42, sha("b"))]));

    fake.pullRequest("synchronize", pull(42, sha("b")));

    const comments = fake.comments(42);
    expect(comments).toHaveLength(1);
    expect(comments[0].body).toContain("`bbbbbbb`");
    expect(comments[0].body).not.toContain("`aaaaaaa`");
  });

  it("leaves other comments alone", () => {
    const fake = app({ pulls: [pull(42, sha("a"))], comments: [{ id: 1, issue: 42, body: "Looks good" }] });
    fake.deliverAnswers(up(42));

    fake.pullRequest("opened", pull(42, sha("a")));

    expect(fake.comments(42)).toHaveLength(2);
    expect(fake.comments(42)[0].body).toBe("Looks good");
  });

  it("says why when the deploy was skipped, with a neutral check", () => {
    const pr = pull(42, sha("a"));
    const fake = app({ pulls: [pr] });
    const reason = "deadlift: no Slot is free for feature-42: feature-7 and feature-9 hold them.";
    fake.deliverAnswers({ outcome: "skipped", reason, environment: "feature-42" });

    const run = fake.pullRequest("opened", pr);

    expect(run.jobs.deliver.result, run.stderr).toBe("success");
    expect(fake.comments(42)[0].body).toContain(reason);
    expect(fake.checks()).toEqual([
      expect.objectContaining({
        head_sha: sha("a"),
        conclusion: "neutral",
        output: expect.objectContaining({ summary: reason }),
      }),
    ]);
  });

  it("says why when the deploy failed, and fails", () => {
    const pr = pull(42, sha("a"));
    const fake = app({ pulls: [pr] });
    const reason = "deadlift: feature-42's routed service web isn't healthy, so feature-42 is stopped.";
    fake.deliverAnswers({ outcome: "failed", reason, environment: "feature-42" });

    const run = fake.pullRequest("opened", pr);

    expect(run.jobs.deliver.result).toBe("failure");
    expect(fake.comments(42)[0].body).toContain(reason);
    expect(fake.comments(42)[0].body).toContain("https://github.com/acme/shop/actions/runs/");
  });
});

describe("deadlift-feature's conditions", () => {
  const skipDeploy = { labels: [{ name: "skip-deploy" }] };

  it("builds and delivers nothing for a pull request with the skip-deploy label", () => {
    const pr = pull(42, sha("a"), skipDeploy);
    const fake = app({ pulls: [pr] });

    const run = fake.pullRequest("synchronize", pr);

    expect(run.used).toEqual([]);
    expect(fake.comments(42)).toEqual([]);
  });

  it("delivers nothing when the label was added while the run waited", () => {
    const fake = app({ pulls: [pull(42, sha("a"), skipDeploy)] });

    const run = fake.pullRequest("synchronize", pull(42, sha("a")));

    expect(run.jobs.deliver.result, run.stderr).toBe("success");
    expect(run.delivered).toEqual([]);
  });

  it("deploys once the skip-deploy label is removed, and not for other labels", () => {
    const pr = pull(42, sha("a"));
    const fake = app({ pulls: [pr] });

    expect(fake.pullRequest("unlabeled", pr, { label: { name: "skip-deploy" } }).delivered).toEqual([
      expect.objectContaining({ operation: "deploy", environment: "feature-42" }),
    ]);
    expect(fake.pullRequest("unlabeled", pr, { label: { name: "docs" } }).used).toEqual([]);
  });

  it("delivers nothing when the pull request closed or moved on while the run waited", () => {
    const fake = app({ pulls: [pull(42, sha("a"), { state: "closed" }), pull(57, sha("c"))] });

    expect(fake.pullRequest("synchronize", pull(42, sha("a"))).delivered).toEqual([]);
    expect(fake.pullRequest("synchronize", pull(57, sha("b"))).delivered).toEqual([]);
  });

  it("delivers nothing, and fails, when it can't list the open pull requests", () => {
    const pr = pull(42, sha("a"));
    const fake = app({ pulls: [pr] });
    fake.github.update((s) => (s.failures = [{ method: "GET", pathPrefix: `repos/${repo}/pulls`, status: 502 }]));

    const run = fake.pullRequest("opened", pr);

    expect(run.jobs.deliver.result).toBe("failure");
    expect(run.delivered).toEqual([]);
  });

  it("does nothing for a pull request from a fork", () => {
    const pr = pull(42, sha("a"), { head: { sha: sha("a"), repo: { full_name: "someone/shop" } } });
    const fake = app({ pulls: [pr] });

    expect(fake.pullRequest("opened", pr).used).toEqual([]);
    expect(fake.pullRequest("closed", { ...pr, state: "closed" }).used).toEqual([]);
  });
});

describe("deadlift-feature on close", () => {
  it("destroys the Feature Environment, and says so in the comment", () => {
    const fake = app({ pulls: [pull(42, sha("a"))] });
    fake.deliverAnswers(up(42));
    fake.pullRequest("opened", pull(42, sha("a")));
    const closed = pull(42, sha("a"), { state: "closed" });
    fake.github.update((s) => (s.repos![repo].pulls = [closed]));
    fake.deliverAnswers({ outcome: "done", reason: "deadlift: destroyed feature-42", environment: "feature-42" });

    const run = fake.pullRequest("closed", closed);

    expect(run.asked("build")).toEqual([]);
    expect(run.delivered).toEqual([
      expect.objectContaining({ app: "shop", server: "deadlift-1", operation: "destroy", environment: "feature-42" }),
    ]);
    expect(fake.comments(42)).toHaveLength(1);
    expect(fake.comments(42)[0].body).toMatch(/destroyed/);
  });

  it("destroys it even with the skip-deploy label, and starts no comment", () => {
    const closed = pull(42, sha("a"), { state: "closed", labels: [{ name: "skip-deploy" }] });
    const fake = app({ pulls: [closed] });

    const run = fake.pullRequest("closed", closed);

    expect(run.delivered).toEqual([expect.objectContaining({ operation: "destroy", environment: "feature-42" })]);
    expect(fake.comments(42)).toEqual([]);
  });

  it("says when the destroy failed, and fails", () => {
    const fake = app({ pulls: [pull(42, sha("a"))] });
    fake.deliverAnswers(up(42));
    fake.pullRequest("opened", pull(42, sha("a")));
    const reason = "deadlift: couldn't reach deadlift-1 over SSH, so nothing was changed.";
    fake.deliverAnswers({ outcome: "failed", reason });

    const run = fake.pullRequest("closed", pull(42, sha("a"), { state: "closed" }));

    expect(run.jobs.deliver.result).toBe("failure");
    expect(fake.comments(42)[0].body).toContain(reason);
  });
});
