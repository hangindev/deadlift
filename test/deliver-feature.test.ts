// The tracer bullet: an App's CI delivers the example App to a Feature Environment.
import { afterAll, afterEach, beforeAll, describe, expect, test } from "vitest";
import { get, startHarness } from "./harness.ts";

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
});
