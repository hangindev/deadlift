// What deadlift requires of an App's Compose template: a routed service it can judge by its
// Compose healthcheck (ADR 0002).
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { exampleTemplate, startHarness } from "./harness.ts";

let harness: Awaited<ReturnType<typeof startHarness>>;
beforeAll(async () => {
  harness = await startHarness();
});
afterEach(() => harness.removeServers());
afterAll(() => harness.remove());

/** The example template with its routed service's healthcheck replaced by `healthcheck`. */
async function withHealthcheck(healthcheck: string) {
  return (await exampleTemplate()).replace(
    /^ {4}healthcheck:\n(?: {6}.*\n)+/m,
    healthcheck ? `    healthcheck:\n${healthcheck}` : "",
  );
}

const refusals: Array<[string, () => Promise<string>, RegExp]> = [
  [
    "has no x-deadlift.route",
    async () => (await exampleTemplate()).replace(/^x-deadlift:\n.*\n/m, ""),
    /x-deadlift\.route/,
  ],
  [
    "routes to a service it doesn't have",
    async () => (await exampleTemplate()).replace("route: web:3000", "route: website:3000"),
    /website/,
  ],
  ["routes to a service with no healthcheck", () => withHealthcheck(""), /no healthcheck/],
  [
    "routes to a service whose healthcheck is disabled",
    () => withHealthcheck("      disable: true\n"),
    /disabled/,
  ],
  [
    "routes to a service whose healthcheck test is NONE",
    () => withHealthcheck('      test: ["NONE"]\n'),
    /disabled/,
  ],
];

test.each(refusals)("a template that %s is refused, and nothing runs", async (_, template, why) => {
  const server = await harness.server();
  const app = server.apps.a.name;

  const result = await server.deliver(
    app,
    ["deploy", "feature-1", "--image", `web=${await harness.image()}`],
    { template: await template() },
  );

  expect(result.code, result.output).toBe(2);
  expect(result.outcome).toBe("failed");
  expect(result.reason).toMatch(/^deadlift: /);
  expect(result.reason).toMatch(why);
  expect(await server.containers(app)).toBe("");
  expect(await server.serveMappings()).toEqual([]);
});
