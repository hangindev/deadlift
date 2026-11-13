// `deadlift server provision`, the wizard that gets a new Server, driven as a maintainer would:
// a scripted human reads what it prints and answers each prompt in turn. The machines it walks
// them through buying, and the tailnet they join, are fakes (test/fakes/provision/): `ssh` to a
// machine and the maintainer's own `tailscale`. Nothing here reaches a real machine or tailnet,
// and no browser opens.
import { spawn } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, test } from "vitest";

const repo = new URL("..", import.meta.url).pathname;
const fakes = join(repo, "test/fakes/provision");

const cleanups: (() => void)[] = [];
afterEach(() => cleanups.splice(0).forEach((cleanup) => cleanup()));

/** One thing the human does: once the wizard has printed `on`, do `then`, and type `answer`. */
type Turn = { on: RegExp; answer?: string; then?: () => void; interrupt?: boolean };

type Run = { code: number | null; output: string };

/**
 * The maintainer's computer and the world around it: machines they can buy, each answering SSH
 * on its public address until its firewall closes it, and the tailnet those machines join.
 */
function world() {
  const dir = mkdtempSync(join(tmpdir(), "deadlift-provision-"));
  cleanups.push(() => rmSync(dir, { recursive: true, force: true }));
  const bin = join(dir, "bin");
  mkdirSync(bin);
  mkdirSync(join(dir, "machines"));
  mkdirSync(join(dir, "home"));
  for (const tool of ["ssh", "tailscale"]) symlinkSync(join(fakes, tool), join(bin, tool));
  for (const opener of ["xdg-open", "open", "wslview", "explorer.exe"]) {
    symlinkSync(join(fakes, "browser"), join(bin, opener));
  }

  const machine = (address: string) => join(dir, "machines", address);
  const read = (path: string) => (existsSync(path) ? readFileSync(path, "utf8") : "");

  return {
    /** A fresh Ubuntu 24.04 machine bought at the provider, at a public IPv4 address. */
    buy(address: string) {
      mkdirSync(machine(address));
    },
    /** Blocks SSH on a machine's public address, as the provider's firewall does. */
    closePublicSsh(address: string) {
      writeFileSync(join(machine(address), "public-ssh-closed"), "");
    },
    /** The routes each bootstrap of the machine came by, in order: `public` or `tailnet`. */
    bootstraps(address: string): string[] {
      return read(join(machine(address), "bootstraps")).split("\n").filter(Boolean);
    },
    /** The machine's name and tags on the tailnet, once it has joined. */
    tailnet(address: string) {
      return {
        name: read(join(machine(address), "hostname")).trim(),
        tags: read(join(machine(address), "tags")).split("\n").filter(Boolean),
      };
    },
    /** The URLs the wizard opened in the browser. */
    opened(): string[] {
      return read(join(dir, "opened")).split("\n").filter(Boolean);
    },

    /** Runs `deadlift server provision`, with the human taking each turn in order. */
    provision(turns: Turn[]): Promise<Run> {
      const child = spawn(join(repo, "bin/deadlift"), ["server", "provision"], {
        env: {
          PATH: `${bin}:/usr/local/bin:/usr/bin:/bin`,
          HOME: join(dir, "home"),
          LANG: "C.UTF-8",
          DEADLIFT_TEST_WORLD: dir,
        },
        stdio: ["pipe", "pipe", "pipe"],
      });
      let output = "";
      let changed = () => {};
      const append = (chunk: Buffer) => {
        output += chunk.toString();
        changed();
      };
      child.stdout.on("data", append);
      child.stderr.on("data", append);
      const exited = new Promise<number | null>((resolve) => child.on("close", (code) => resolve(code)));
      let done = false;
      exited.then(() => {
        done = true;
        changed();
      });

      return (async () => {
        let cursor = 0;
        for (const turn of turns) {
          const match = await new Promise<RegExpExecArray>((resolve, reject) => {
            const timer = setTimeout(() => fail("timed out"), 20_000);
            const fail = (why: string) => {
              clearTimeout(timer);
              reject(new Error(`${why} waiting for ${turn.on} after:\n${output.slice(cursor)}`));
            };
            changed = () => {
              const found = turn.on.exec(output.slice(cursor));
              if (found) {
                clearTimeout(timer);
                resolve(found);
              } else if (done) fail("the wizard exited");
            };
            changed();
          });
          cursor += match.index + match[0].length;
          turn.then?.();
          if (turn.interrupt) {
            child.kill("SIGINT");
            break;
          }
          child.stdin.write(`${turn.answer ?? ""}\n`);
        }
        changed = () => {};
        child.stdin.end();
        return { code: await exited, output };
      })();
    },
  };
}

/** The stage titles a run showed, in order. */
function stages(output: string): string[] {
  return [...output.matchAll(/▸ Stage \d+\/\d+ · (.*)/g)].map((match) => match[1]);
}

/** The policy entries printed, parsed: HuJSON is JSON with comments and trailing commas. */
function printedPolicy(output: string) {
  const found = /\n\n(    \{\n[\s\S]*?\n    \})\n/.exec(output);
  if (!found) throw new Error(`no policy printed in:\n${output}`);
  const json = found[1].replace(/^\s*\/\/.*$/gm, "").replace(/,(\s*[}\]])/g, "$1");
  return JSON.parse(json);
}

/** Every turn of a first run, from start to finish, for a Server named `name` at `address`. */
function wholeRun(w: ReturnType<typeof world>, name: string, address: string, policy: Turn): Turn[] {
  return [
    { on: /Ready to start\?/ },
    policy,
    { on: /such as deadlift-1/, answer: name },
    { on: /Create & Buy now/, answer: address, then: () => w.buy(address) },
    { on: /Created and applied\?/ },
    { on: /Bootstrapped\? Continue\?/ },
    { on: /Copy the Tailnet DNS name/, answer: "tailtest.ts.net" },
    { on: /Continue\?/ },
    { on: /Deleted\?/, then: () => w.closePublicSsh(address) },
  ];
}

describe("deadlift server provision", () => {
  test("walks through the policy, buying, the provider firewall, bootstrap, joining the tailnet and closing public SSH", async () => {
    const w = world();
    const run = await w.provision(wholeRun(w, "deadlift-1", "203.0.113.7", { on: /Saved\?/ }));

    expect(run.code, run.output).toBe(0);
    expect(stages(run.output)).toEqual([
      "Tailscale: the tailnet policy",
      "Hetzner: buy the Server",
      "Hetzner: the firewall",
      "Bootstrap the Server",
      "Tailscale: join the tailnet",
      "Hetzner: close public SSH",
    ]);
    expect(w.bootstraps("203.0.113.7")).toEqual(["public"]);
    expect(w.tailnet("203.0.113.7")).toEqual({ name: "deadlift-1", tags: ["tag:deadlift-server"] });
    expect(run.output).toMatch(/SSH on 203\.0\.113\.7 no longer answers/);
    expect(run.output).toMatch(/deadlift app add <app> --server deadlift-1 --repo <owner\/name>/);
  });
});
