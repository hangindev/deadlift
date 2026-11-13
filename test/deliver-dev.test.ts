// The Dev Environment: an App's CI delivers each merge to it, at the root of the App's Service.
import { afterAll, afterEach, beforeAll, describe, expect, test } from "vitest";
import { get, startHarness } from "./harness.ts";

let harness: Awaited<ReturnType<typeof startHarness>>;
beforeAll(async () => {
  harness = await startHarness();
});
afterEach(() => harness.removeServers());
afterAll(() => harness.remove());

describe("deploy dev", () => {
  test("brings the example App up healthy at the root of the App's Service, and deliver reports its URL", async () => {
    const server = await harness.server();
    const app = server.apps.a.name;

    const result = await server.deliver(app, [
      "deploy",
      "dev",
      "--image",
      `web=${await harness.image("healthy", 1)}`,
    ]);

    expect(result.code, result.output).toBe(0);
    expect(result.outcome).toBe("done");
    expect(result.environment).toBe("dev");
    expect(result.url).toBe(`https://${app}.tailtest.ts.net/`);
    expect(result.reason).toMatch(/^deadlift: /);
    expect(await get(server.devPort("a"), "/version")).toBe("1");
    expect(await server.serveMappings()).toEqual([
      { service: `svc:${app}`, port: 443, target: `http://127.0.0.1:${server.devPort("a")}` },
    ]);
  });
});
