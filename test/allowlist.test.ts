// What an App's Compose template may hold (ADR 0001): anything that reaches past the App's own
// Compose project is refused before anything stops.
import { afterAll, beforeAll, describe, expect, test } from "vitest";
import { exampleTemplate, get, startHarness } from "./harness.ts";

let harness: Awaited<ReturnType<typeof startHarness>>;
beforeAll(async () => {
  harness = await startHarness();
});
afterAll(() => harness.remove());

/** The example template with `lines` added to its routed service, web. */
async function inWeb(lines: string) {
  const template = await exampleTemplate();
  const web = "\n  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n";
  if (!template.includes(web)) throw new Error("the example template's web service has moved");
  return template.replace(web, `${web}${lines}`);
}

/** The example template with `text` added at its top level. */
async function atTopLevel(text: string) {
  return `${await exampleTemplate()}\n${text}`;
}

const refusals: Array<[string, () => Promise<string>, RegExp]> = [
  ["a privileged container", () => inWeb("    privileged: true\n"), /privileged/],
];

describe("a template ADR 0001 refuses", () => {
  let server: Awaited<ReturnType<typeof harness.server>>;
  let app: string;
  let running: string;

  // One Feature Environment is up throughout, and every refused delivery targets it.
  beforeAll(async () => {
    server = await harness.server();
    app = server.apps.a.name;
    const result = await server.deliver(app, [
      "deploy",
      "feature-1",
      "--image",
      `web=${await harness.image("healthy", 1)}`,
    ]);
    expect(result.outcome, result.output).toBe("done");
    running = await containerIds(app);
  });
  afterAll(() => harness.removeServers());

  async function containerIds(name: string) {
    return (await server.runningContainers(name)).split("\n").sort().join("\n");
  }

  test.each(refusals)(
    "is refused for %s, exit 2, naming the key, and the running Environment is unchanged",
    async (_, template, key) => {
      const mappings = await server.serveMappings();

      const result = await server.deliver(
        app,
        ["deploy", "feature-1", "--image", `web=${await harness.image("healthy", 2)}`],
        { template: await template() },
      );

      expect(result.code, result.output).toBe(2);
      expect(result.outcome).toBe("failed");
      expect(result.reason).toMatch(/^deadlift: /);
      expect(result.reason).toMatch(key);
      expect(result.reason).toMatch(/ADR 0001/);
      expect(await containerIds(app)).toBe(running);
      expect(await get(server.slotPort("a", 1), "/version")).toBe("1");
      expect(await server.serveMappings()).toEqual(mappings);
    },
  );
});

// Silences "unused" until the next tests use it.
void atTopLevel;
