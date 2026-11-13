// The tracer bullet: an App's CI delivers the example App to a Feature Environment.
import { afterAll, afterEach, beforeAll, describe, expect, test } from "vitest";
import { exampleTemplate, get, startHarness } from "./harness.ts";

let harness: Awaited<ReturnType<typeof startHarness>>;
beforeAll(async () => {
  harness = await startHarness();
});
afterEach(() => harness.removeServers());
afterAll(() => harness.remove());

describe("deploy feature-<key>", () => {
  test("brings the example App up healthy on its Slot, and deliver reports done with its URL", async () => {
    const server = await harness.server();
    const app = server.apps.a.name;

    const result = await server.deliver(app, [
      "deploy",
      "feature-42",
      "--image",
      `web=${await harness.image("healthy", 1)}`,
    ]);

    expect(result.code, result.output).toBe(0);
    expect(result.outcome).toBe("done");
    expect(result.environment).toBe("feature-42");
    // The first Slot's public port on the App's own Service name (ADR 0003).
    expect(result.url).toBe(`https://${app}.tailtest.ts.net:10001/`);
    expect(result.reason).toMatch(/^deadlift: /);
    expect(await get(server.slotPort("a", 1), "/version")).toBe("1");
  });

  test("explains itself in the job summary", async () => {
    const server = await harness.server();
    const app = server.apps.a.name;

    const result = await server.deliver(app, ["deploy", "feature-42", "--image", `web=${await harness.image()}`]);

    expect(result.summary).toContain("deadlift deploy feature-42: done");
    expect(result.summary).toContain(result.reason!);
    expect(result.summary).toContain(`Address: https://${app}.tailtest.ts.net:10001/`);
  });

  test("maps the App's Service to the Environment's loopback port", async () => {
    const server = await harness.server();
    const app = server.apps.a.name;

    await server.deliver(app, ["deploy", "feature-42", "--image", `web=${await harness.image()}`]);

    expect(await server.serveMappings()).toEqual([
      {
        service: `svc:${app}`,
        port: 10001,
        target: `http://127.0.0.1:${server.slotPort("a", 1)}`,
      },
    ]);
  });

  test("gives each named image to the template as ${DEADLIFT_IMAGE_<NAME>}", async () => {
    const server = await harness.server();
    const app = server.apps.a.name;
    // The routed service runs the image named `site`, and its migration the one named `migrator`.
    const template = (await exampleTemplate())
      .replace(/(\n {2}web:\n {4}image: )\$\{DEADLIFT_IMAGE_WEB\}/, "$1${DEADLIFT_IMAGE_SITE}")
      .replace("${DEADLIFT_IMAGE_WEB}", "${DEADLIFT_IMAGE_MIGRATOR}");

    const result = await server.deliver(
      app,
      [
        "deploy",
        "feature-42",
        "--image",
        `migrator=${await harness.image("healthy", 1)}`,
        "--image",
        `site=${await harness.image("healthy", 2)}`,
      ],
      { template },
    );

    expect(result.outcome, result.output).toBe("done");
    expect(await get(server.slotPort("a", 1), "/version")).toBe("2");
  });

  test("goes ahead when a one-shot service that nothing depends on exits 0", async () => {
    const server = await harness.server();
    const app = server.apps.a.name;
    // `docker compose up --wait` over the whole project fails on this one, so deadlift waits on
    // the routed service only.
    const template = (await exampleTemplate()).replace(
      "services:\n",
      'services:\n  seed:\n    image: ${DEADLIFT_IMAGE_WEB}\n    command: ["true"]\n    restart: "no"\n',
    );

    const result = await server.deliver(
      app,
      ["deploy", "feature-42", "--image", `web=${await harness.image()}`],
      { template },
    );

    expect(result.outcome, result.output).toBe("done");
    expect(await get(server.slotPort("a", 1), "/version")).toBe("1");
  });

  test.each([
    ["failing-migration", /migrate/],
    ["unhealthy", /unhealthy/],
  ] as const)("fails, exit 1, with a %s image, and leaves the Environment stopped", async (kind, why) => {
    const server = await harness.server();
    const app = server.apps.a.name;

    const result = await server.deliver(app, [
      "deploy",
      "feature-42",
      "--image",
      `web=${await harness.image(kind)}`,
    ]);

    expect(result.code, result.output).toBe(1);
    expect(result.outcome).toBe("failed");
    expect(result.reason).toMatch(/^deadlift: .*feature-42/);
    expect(result.reason).toMatch(why);
    expect(result.reason).toMatch(/stopped/);
    expect(await server.runningContainers(app)).toBe("");
  });
});
