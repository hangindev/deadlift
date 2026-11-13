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

test("one App's secrets never reach another App's Environments", async () => {
  const server = await harness.server();
  const [a, b] = [server.apps.a.name, server.apps.b.name];
  await server.secrets(a, "feature", "GREETING=a's greeting\nA_ONLY=a's own secret\n");
  await server.secrets(b, "feature", "GREETING=b's greeting\n");
  const template = await templateWithEnvironment({ GREETING: "${GREETING}", A_ONLY: "${A_ONLY}" });

  const result = await server.deliver(
    b,
    ["deploy", "feature-42", "--image", `web=${await harness.image()}`],
    { template },
  );

  expect(result.outcome, result.output).toBe("done");
  expect(await get(server.slotPort("b", 1), "/env/GREETING")).toBe("b's greeting");
  expect(await get(server.slotPort("b", 1), "/env/A_ONLY")).toBe("");
});

test("a secrets file can't set deadlift's own variables", async () => {
  const server = await harness.server();
  const app = server.apps.a.name;
  await server.secrets(app, "feature", "DEADLIFT_ENV=dev\nDEADLIFT_APP=someone-else\n");
  const template = await templateWithEnvironment({
    ENVIRONMENT: "${DEADLIFT_ENV}",
    APP: "${DEADLIFT_APP}",
  });

  const result = await server.deliver(
    app,
    ["deploy", "feature-42", "--image", `web=${await harness.image()}`],
    { template },
  );

  expect(result.outcome, result.output).toBe("done");
  expect(await get(server.slotPort("a", 1), "/env/ENVIRONMENT")).toBe("feature-42");
  expect(await get(server.slotPort("a", 1), "/env/APP")).toBe(app);
});

test("a deploy fails, changing nothing, when the App's secrets file isn't a valid env file", async () => {
  const server = await harness.server();
  const app = server.apps.a.name;
  await server.secrets(app, "feature", "GREETING=hello\nnot a variable\n");

  const result = await server.deliver(app, [
    "deploy",
    "feature-42",
    "--image",
    `web=${await harness.image()}`,
  ]);

  expect(result.code, result.output).toBe(1);
  expect(result.outcome).toBe("failed");
  expect(result.reason).toMatch(/^deadlift: .*feature\.env.*line 2/);
  expect(result.reason).toMatch(/deadlift app secrets/);
  expect(await server.containers(app)).toBe("");
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

test("DEADLIFT_SECRET is random per Environment, and the same across redeploys", async () => {
  const server = await harness.server();
  const app = server.apps.a.name;
  const template = await templateWithEnvironment({ SECRET: "${DEADLIFT_SECRET}" });
  const deploy = async (key: string) => {
    const result = await server.deliver(
      app,
      ["deploy", `feature-${key}`, "--image", `web=${await harness.image()}`],
      { template },
    );
    expect(result.outcome, result.output).toBe("done");
  };

  await deploy("42");
  await deploy("43");
  const first = await get(server.slotPort("a", 1), "/env/SECRET");
  const second = await get(server.slotPort("a", 2), "/env/SECRET");
  await deploy("42");
  const redeployed = await get(server.slotPort("a", 1), "/env/SECRET");

  expect(first).toMatch(/^[0-9a-f]{64}$/);
  expect(second).toMatch(/^[0-9a-f]{64}$/);
  expect(second).not.toBe(first);
  expect(redeployed).toBe(first);
});

// Skipped until the destroy operation lands (#6): then un-skip it.
test.skip("DEADLIFT_SECRET is new after the Environment is destroyed and deployed again", async () => {
  const server = await harness.server();
  const app = server.apps.a.name;
  const template = await templateWithEnvironment({ SECRET: "${DEADLIFT_SECRET}" });
  const deploy = async () => {
    const result = await server.deliver(
      app,
      ["deploy", "feature-42", "--image", `web=${await harness.image()}`],
      { template },
    );
    expect(result.outcome, result.output).toBe("done");
    return get(server.slotPort("a", 1), "/env/SECRET");
  };

  const before = await deploy();
  const destroyed = await server.deliver(app, ["destroy", "feature-42"]);
  expect(destroyed.outcome, destroyed.output).toBe("done");
  const after = await deploy();

  expect(after).toMatch(/^[0-9a-f]{64}$/);
  expect(after).not.toBe(before);
});
