// The entry command: what an App's CI can and can't make the Server do, whatever it sends.
import { mkdtemp, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, afterEach, beforeAll, describe, expect, test } from "vitest";
import { exampleTemplate, get, startHarness, tarOf } from "./harness.ts";

let harness: Awaited<ReturnType<typeof startHarness>>;
let scratch: string;
beforeAll(async () => {
  harness = await startHarness();
  scratch = await mkdtemp(join(tmpdir(), "deadlift-test-entry-"));
});
afterEach(() => harness.removeServers());
afterAll(async () => {
  await harness.remove();
  await rm(scratch, { recursive: true, force: true });
});

const exists = (path: string) => stat(path).then(
  () => true,
  () => false,
);

describe("a delivery that isn't a tar of only compose.yaml and the token", () => {
  const marker = () => join(scratch, "delivered-code-ran");
  const cases: Array<[string, () => Promise<Buffer>]> = [
    ["bytes that aren't a tar", async () => Buffer.from("not a tar archive\n".repeat(64))],
    ["an empty stdin", async () => Buffer.alloc(0)],
    [
      "a deploy script beside the template",
      async () =>
        tarOf({
          "compose.yaml": await exampleTemplate(),
          "deploy.sh": `#!/bin/sh\ntouch ${marker()}\n`,
        }),
    ],
    [
      "a directory",
      async () => tarOf({ "compose.yaml": await exampleTemplate(), extra: { directory: true } }),
    ],
    ["a symlink as its template", async () => tarOf({ "compose.yaml": { symlink: "/etc/passwd" } })],
  ];

  test.each(cases)("is refused, exit 2, with nothing run: %s", async (_, delivery) => {
    const server = await harness.server();
    const app = server.apps.a.name;

    const result = await server.send(
      `deadlift-${app}`,
      ["--protocol", "1", "deploy", "feature-1", "--image", `web=${await harness.image()}`],
      await delivery(),
    );

    expect(result.code, result.output).toBe(2);
    expect(result.output).toMatch(/^deadlift: refused the delivery: .*Nothing was run\.$/m);
    expect(await exists(marker())).toBe(false);
    expect(await server.containers(app)).toBe("");
    expect(await server.tailscaleCalls()).toEqual([]);
  });
});

describe("the delivery protocol", () => {
  async function sendWith(protocol: string[]) {
    const server = await harness.server();
    const app = server.apps.a.name;
    const result = await server.send(
      `deadlift-${app}`,
      [...protocol, "deploy", "feature-1", "--image", `web=${await harness.image()}`],
      await tarOf({ "compose.yaml": await exampleTemplate() }),
    );
    return { result, containers: await server.containers(app) };
  }

  test("one the Server doesn't support is refused, exit 2, saying to re-run bootstrap", async () => {
    const { result, containers } = await sendWith(["--protocol", "2"]);

    expect(result.code, result.output).toBe(2);
    expect(result.output).toMatch(/^deadlift: .*protocol '2'.*re-run `deadlift server bootstrap`/im);
    expect(containers).toBe("");
  });

  test("none at all is refused, exit 2", async () => {
    const { result, containers } = await sendWith([]);

    expect(result.code, result.output).toBe(2);
    expect(result.output).toMatch(/^deadlift: refused the delivery: .*protocol/m);
    expect(containers).toBe("");
  });
});

test("nothing in a delivery is executed, the token included", async () => {
  const server = await harness.server();
  const app = server.apps.a.name;
  const marker = join(scratch, "token-ran");

  const result = await server.deliver(
    app,
    ["deploy", "feature-1", "--image", `web=${await harness.image()}`],
    { token: `$(touch ${marker})\`touch ${marker}\`` },
  );

  expect(result.outcome, result.output).toBe("done");
  expect(await exists(marker)).toBe(false);
});

describe("the App is worked out from SUDO_USER", () => {
  test("another App's deploy user deploying the same key gets its own Environment", async () => {
    const server = await harness.server();
    const [a, b] = [server.apps.a.name, server.apps.b.name];
    await server.deliver(a, ["deploy", "feature-1", "--image", `web=${await harness.image("healthy", 1)}`]);

    const result = await server.deliver(b, [
      "deploy",
      "feature-1",
      "--image",
      `web=${await harness.image("healthy", 2)}`,
    ]);

    expect(result.outcome, result.output).toBe("done");
    expect(result.url).toBe(`https://${b}.tailtest.ts.net:10001/`);
    expect(await get(server.slotPort("a", 1), "/version")).toBe("1");
    expect(await get(server.slotPort("b", 1), "/version")).toBe("2");
    expect(await server.serveMappings()).toEqual(
      expect.arrayContaining([
        { service: `svc:${a}`, port: 10001, target: `http://127.0.0.1:${server.slotPort("a", 1)}` },
        { service: `svc:${b}`, port: 10001, target: `http://127.0.0.1:${server.slotPort("b", 1)}` },
      ]),
    );
  });

  const crossings: Array<[string, (a: string) => string[]]> = [
    ["--app", (a) => ["--app", a]],
    ["an Environment outside its own", (a) => [`../${a}/feature-1`]],
  ];

  test.each(crossings)("arguments naming another App (%s) are refused, exit 2", async (_, extra) => {
    const server = await harness.server();
    const [a, b] = [server.apps.a.name, server.apps.b.name];
    await server.deliver(a, ["deploy", "feature-1", "--image", `web=${await harness.image()}`]);
    const before = await server.serveMappings();

    const args = extra(a);
    const result = await server.deliver(
      b,
      args[0]?.startsWith("..")
        ? ["deploy", ...args, "--image", `web=${await harness.image("healthy", 2)}`]
        : ["deploy", "feature-1", "--image", `web=${await harness.image("healthy", 2)}`, ...args],
    );

    expect(result.code, result.output).toBe(2);
    expect(result.reason).toMatch(/^deadlift: /);
    expect(await get(server.slotPort("a", 1), "/version")).toBe("1");
    expect(await server.serveMappings()).toEqual(before);
    expect(await server.containers(b)).toBe("");
  });

  test.each(["deadlift-nobody", "root"])(
    "a user that isn't an App's deploy user (%s) is refused, exit 2",
    async (user) => {
      const server = await harness.server();

      const result = await server.send(
        user,
        ["--protocol", "1", "deploy", "feature-1", "--image", `web=${await harness.image()}`],
        await tarOf({ "compose.yaml": await exampleTemplate() }),
      );

      expect(result.code, result.output).toBe(2);
      expect(result.output).toMatch(/^deadlift: refused the delivery: .*isn't the deploy user/m);
      expect(await server.tailscaleCalls()).toEqual([]);
    },
  );
});

test("operations on one Server run one at a time", async () => {
  const server = await harness.server();
  const app = server.apps.a.name;
  const ref = await harness.image();
  const release = await server.holdLock();

  let finished = false;
  const delivery = server.deliver(app, ["deploy", "feature-1", "--image", `web=${ref}`]).then(
    (result) => {
      finished = true;
      return result;
    },
  );
  await new Promise((resolve) => setTimeout(resolve, 3000));
  const whileHeld = { finished, containers: await server.containers(app) };
  await release();
  const result = await delivery;

  expect(whileHeld).toEqual({ finished: false, containers: "" });
  expect(result.outcome, result.output).toBe("done");
});

test("a deploy with no free Slot is skipped, exit 75, and deliver doesn't fail the step", async () => {
  const server = await harness.server({ slots: 1 });
  const app = server.apps.a.name;
  const ref = await harness.image();
  await server.deliver(app, ["deploy", "feature-1", "--image", `web=${ref}`]);

  const result = await server.deliver(app, ["deploy", "feature-2", "--image", `web=${ref}`]);

  expect(result.code, result.output).toBe(0);
  expect(result.outcome).toBe("skipped");
  expect(result.reason).toMatch(/^deadlift: .*held, by feature-1, so feature-2 wasn't deployed/);
  const direct = await server.send(
    `deadlift-${app}`,
    ["--protocol", "1", "deploy", "feature-2", "--image", `web=${ref}`],
    await tarOf({ "compose.yaml": await exampleTemplate() }),
  );
  expect(direct.code, direct.output).toBe(75);
});

// Exit codes as the entry command gives them, before deliver maps them to outcomes.
test.each([
  ["done", "healthy", 0],
  ["failed", "failing-migration", 1],
] as const)("the entry command exits as documented when %s", async (_, kind, code) => {
  const server = await harness.server();
  const app = server.apps.a.name;

  const result = await server.send(
    `deadlift-${app}`,
    ["--protocol", "1", "deploy", "feature-1", "--image", `web=${await harness.image(kind)}`],
    await tarOf({ "compose.yaml": await exampleTemplate() }),
  );

  expect(result.code, result.output).toBe(code);
  expect(result.output.trimEnd().split("\n").at(-1)).toMatch(/^deadlift: /);
});
