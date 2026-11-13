// The deliver action, run through its action.yml on a fake runner. Its `ssh` stands in for the
// Server, so these tests check what the action sends and how it reports the Server's answer;
// the delivery tests (deliver-*.test.ts) check what the Server does with a delivery.
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { fakeRunner } from "./support/fake-runner.ts";

const cleanups: (() => void)[] = [];
afterEach(() => cleanups.splice(0).forEach((cleanup) => cleanup()));

const template = "services:\n  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n";

/** A runner whose checkout holds the App's compose.yaml, and a stand-in for the tailnet join. */
function runner() {
  const fake = fakeRunner();
  cleanups.push(fake.cleanup);
  writeFileSync(join(fake.workspace, "compose.yaml"), template);
  const joins: Record<string, string>[] = [];
  return {
    ...fake,
    joins,
    deliver(inputs: Record<string, string>, { joinStatus = 0 } = {}) {
      return fake.runAction(
        "actions/deliver",
        {
          app: "shop",
          server: "deadlift-1",
          "oauth-client-id": "client-123",
          audience: "api.tailscale.com/client-123",
          ...inputs,
        },
        {
          uses: {
            "tailscale/github-action": (with_) => {
              joins.push(with_);
              return joinStatus;
            },
          },
        },
      );
    },
  };
}

const deployed = [
  "deadlift-output: environment=feature-42",
  "deadlift-output: url=https://shop.tailtest.ts.net:10001/",
  "deadlift: deployed feature-42, which is up at https://shop.tailtest.ts.net:10001/",
  "",
].join("\n");

describe("the deliver action", () => {
  it("joins the tailnet as the App's CI, delivers as its deploy user, and leaves", () => {
    const fake = runner();
    fake.serverAnswers(deployed);

    const run = fake.deliver({
      operation: "deploy",
      environment: "feature-42",
      images: "web=ghcr.io/acme/shop/web:sha-1234\nworker=ghcr.io/acme/shop/worker:sha-1234\n",
    });

    expect(run.status, run.stderr).toBe(0);
    expect(fake.joins).toEqual([
      {
        "oauth-client-id": "client-123",
        audience: "api.tailscale.com/client-123",
        tags: "tag:deadlift-ci-shop",
        ping: "deadlift-1",
      },
    ]);
    expect(fake.deliveries()).toEqual([
      {
        destination: "deadlift-shop@deadlift-1",
        command: [
          "sudo",
          "--non-interactive",
          "/usr/local/sbin/deadlift-entry",
          "--protocol",
          "1",
          "deploy",
          "feature-42",
          "--image",
          "web=ghcr.io/acme/shop/web:sha-1234",
          "--image",
          "worker=ghcr.io/acme/shop/worker:sha-1234",
        ],
        files: { "compose.yaml": template, token: "fake-job-token" },
      },
    ]);
    expect(fake.calls()).toContain("sudo tailscale logout");
    expect(run.outputs).toEqual({
      outcome: "done",
      reason: "deadlift: deployed feature-42, which is up at https://shop.tailtest.ts.net:10001/",
      url: "https://shop.tailtest.ts.net:10001/",
      environment: "feature-42",
      swept: "",
    });
  });

  /** The words of the operation the Server was asked to run, after the entry command's own. */
  function operationSent(fake: ReturnType<typeof runner>) {
    const [delivery] = fake.deliveries();
    return delivery.command.slice(delivery.command.indexOf("--protocol") + 2);
  }

  it.each([
    [
      "a Sweep keeping some keys",
      { operation: "sweep", keep: "42, 57\n61\n" },
      ["sweep", "--keep", "42,57,61"],
    ],
    ["a Sweep keeping none", { operation: "sweep", "keep-none": "true" }, ["sweep", "--keep-none"]],
    [
      "a Feature deploy that sweeps first",
      { operation: "deploy", environment: "feature-42", images: "web=x:1", keep: "42,57" },
      ["deploy", "feature-42", "--image", "web=x:1", "--keep", "42,57"],
    ],
    ["a destroy", { operation: "destroy", environment: "feature-42" }, ["destroy", "feature-42"]],
    ["a Rollback", { operation: "rollback", environment: "dev" }, ["rollback", "dev"]],
    [
      "a Rollback that may discard newer data",
      { operation: "rollback", environment: "dev", "discard-newer-data": "true" },
      ["rollback", "dev", "--discard-newer-data"],
    ],
  ])("delivers %s", (_, inputs, expected) => {
    const fake = runner();

    const run = fake.deliver(inputs);

    expect(run.status, run.stderr).toBe(0);
    expect(operationSent(fake)).toEqual(expected);
  });

  it("sends the template only with a deploy", () => {
    const fake = runner();

    fake.deliver({ operation: "destroy", environment: "feature-42" });

    expect(Object.keys(fake.deliveries()[0].files)).toEqual(["token"]);
  });

  it.each(["keep-none", "discard-newer-data"])("refuses a %s that isn't true or false", (input) => {
    const fake = runner();

    const run = fake.deliver({ operation: "sweep", [input]: "yes" });

    expect(run.status).toBe(2);
    expect(run.stderr).toMatch(new RegExp(`^deadlift: ${input} must be true or false, not 'yes'`, "m"));
    expect(fake.deliveries()).toEqual([]);
  });

  it("explains the delivery in the job summary", () => {
    const fake = runner();
    fake.serverAnswers(`deadlift-output: swept=7,9\n${deployed}`);

    const run = fake.deliver({ operation: "deploy", environment: "feature-42", images: "web=x:1", keep: "42" });

    expect(run.summary).toContain("deadlift deploy feature-42: done");
    expect(run.summary).toContain(
      "deadlift: deployed feature-42, which is up at https://shop.tailtest.ts.net:10001/",
    );
    expect(run.summary).toMatch(/Address: https:\/\/shop\.tailtest\.ts\.net:10001\//);
    expect(run.summary).toMatch(/Swept: 7, 9/);
  });

  it("explains a failure in the job summary, even when the Server gave no reason", () => {
    const fake = runner();
    fake.serverAnswers("Killed\n", 1);

    const run = fake.deliver({ operation: "destroy", environment: "feature-42" });

    expect(run.summary).toContain("deadlift destroy feature-42: failed");
    expect(run.summary).toMatch(/gave no reason/);
  });

  it("passes a skipped delivery, saying why", () => {
    const fake = runner();
    const reason = "deadlift: not deployed, because no Slot is free: feature-7, feature-9 hold them all";
    fake.serverAnswers(`${reason}\n`, 75);

    const run = fake.deliver({ operation: "deploy", environment: "feature-42", images: "web=x:1" });

    expect(run.status, run.stderr).toBe(0);
    expect(run.outputs).toMatchObject({ outcome: "skipped", reason });
  });

  it.each([
    ["failed", 1],
    ["was refused", 2],
  ])("fails the step when the delivery %s, saying why, and still leaves", (_, status) => {
    const fake = runner();
    const reason = "deadlift: feature-42 didn't become healthy, so it's stopped";
    fake.serverAnswers(`${reason}\n`, status);

    const run = fake.deliver({ operation: "deploy", environment: "feature-42", images: "web=x:1" });

    expect(run.status).toBe(status);
    expect(run.outputs).toMatchObject({ outcome: "failed", reason });
    expect(fake.calls()).toContain("sudo tailscale logout");
  });

  it("fails without delivering when it can't join the tailnet, and still leaves", () => {
    const fake = runner();

    const run = fake.deliver({ operation: "destroy", environment: "feature-42" }, { joinStatus: 1 });

    expect(run.status).not.toBe(0);
    expect(fake.deliveries()).toEqual([]);
    expect(fake.calls()).toContain("sudo tailscale logout");
  });
});
