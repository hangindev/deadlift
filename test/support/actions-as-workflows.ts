// actionlint checks workflows, not composite actions. This writes each of deadlift's actions
// (actions/*/action.yml) out as a workflow it can check: a reusable workflow whose inputs are the
// action's, with one job running the action's steps and giving the action's outputs. actionlint
// then checks the steps' expressions, the inputs of the actions they use, and shellcheck over
// every `run:` script.
//
//   node test/support/actions-as-workflows.ts <directory>
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { parse, stringify } from "yaml";

const repoRoot = resolve(import.meta.dirname, "../..");
const out = process.argv[2];
if (!out) {
  process.stderr.write("usage: node test/support/actions-as-workflows.ts <directory>\n");
  process.exit(2);
}

for (const name of readdirSync(join(repoRoot, "actions")).sort()) {
  const action = parse(readFileSync(join(repoRoot, "actions", name, "action.yml"), "utf8"));
  const outputs = Object.fromEntries(
    Object.entries(action.outputs ?? {}).map(([output, spec]) => [output, (spec as { value: string }).value]),
  );
  const workflow = {
    name: `${action.name} (action.yml as a workflow, for actionlint)`,
    on: {
      workflow_call: {
        inputs: Object.fromEntries(
          Object.entries(action.inputs ?? {}).map(([input, spec]) => [
            input,
            { type: "string", required: Boolean((spec as { required?: boolean }).required) },
          ]),
        ),
      },
    },
    jobs: {
      action: {
        "runs-on": "ubuntu-24.04",
        // actionlint refuses an empty section.
        ...(Object.keys(outputs).length ? { outputs } : {}),
        steps: action.runs.steps,
      },
    },
  };
  writeFileSync(join(out, `action-${name}.yml`), stringify(workflow));
}
