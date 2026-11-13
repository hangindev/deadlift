// The deliver action, run through its action.yml on a fake runner. Its `ssh` stands in for the
// Server, so these tests check what the action sends and how it reports the Server's answer;
// Seam A (deliver-*.test.ts) checks what the Server does with a delivery.
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
});
