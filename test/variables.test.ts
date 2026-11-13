// Variables reaching an App's containers: its template gets the App's secrets file and deadlift's
// own variables through `${VAR}` substitution, and nothing else.
import { afterAll, afterEach, beforeAll, expect, test } from "vitest";
import { exampleTemplate, get, startHarness } from "./harness.ts";

let harness: Awaited<ReturnType<typeof startHarness>>;
beforeAll(async () => {
  harness = await startHarness();
});
afterEach(() => harness.removeServers());
afterAll(() => harness.remove());

/** The example App's template, with its routed service given `environment`, so the container's
 * variables can be read back at `/env/<NAME>`. */
async function templateWithEnvironment(environment: Record<string, string>): Promise<string> {
  const lines = Object.entries(environment).map(([name, value]) => `      ${name}: "${value}"\n`);
  return (await exampleTemplate()).replace(
    "  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n",
    `  web:\n    image: \${DEADLIFT_IMAGE_WEB}\n    environment:\n${lines.join("")}`,
  );
}

test("variables from the App's feature.env reach its Feature Environments", async () => {
  const server = await harness.server();
  const app = server.apps.a.name;
  await server.secrets(app, "feature", "GREETING=hello from feature.env\n");
  const template = await templateWithEnvironment({ GREETING: "${GREETING}" });

  const result = await server.deliver(
    app,
    ["deploy", "feature-42", "--image", `web=${await harness.image()}`],
    { template },
  );

  expect(result.outcome, result.output).toBe("done");
  expect(await get(server.slotPort("a", 1), "/env/GREETING")).toBe("hello from feature.env");
});

test("variables from the App's dev.env don't reach its Feature Environments", async () => {
  const server = await harness.server();
  const app = server.apps.a.name;
  await server.secrets(app, "dev", "GREETING=hello from dev.env\n");
  const template = await templateWithEnvironment({ GREETING: "${GREETING}" });

  const result = await server.deliver(
    app,
    ["deploy", "feature-42", "--image", `web=${await harness.image()}`],
    { template },
  );

  expect(result.outcome, result.output).toBe("done");
  expect(await get(server.slotPort("a", 1), "/env/GREETING")).toBe("");
});

test("DEADLIFT_ENV, DEADLIFT_APP and DEADLIFT_URL hold the Environment, the App and its URL", async () => {
  const server = await harness.server();
  const app = server.apps.a.name;
  const template = await templateWithEnvironment({
    ENVIRONMENT: "${DEADLIFT_ENV}",
    APP: "${DEADLIFT_APP}",
    URL: "${DEADLIFT_URL}",
  });

  const result = await server.deliver(
    app,
    ["deploy", "feature-42", "--image", `web=${await harness.image()}`],
    { template },
  );

  expect(result.outcome, result.output).toBe("done");
  const port = server.slotPort("a", 1);
  expect(await get(port, "/env/ENVIRONMENT")).toBe("feature-42");
  expect(await get(port, "/env/APP")).toBe(app);
  expect(await get(port, "/env/URL")).toBe(`https://${app}.tailtest.ts.net:10001/`);
});
