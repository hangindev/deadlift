// The example App's migration: brings the schema version kept in /data/schema up to its own
// VERSION. It fails, changing nothing, when built with MIGRATE=fail, or when the data's schema is
// newer than this version, as a real App's migration must (ADR 0002).
import { readFileSync, writeFileSync } from "node:fs";

const file = "/data/schema";
const version = Number(process.env.EXAMPLE_VERSION);
let current = 0;
try {
  current = Number(readFileSync(file, "utf8"));
} catch {
  // No data yet: a new Environment.
}

if (process.env.EXAMPLE_MIGRATE === "fail") {
  console.error("Example migration failing on purpose");
  process.exit(1);
}
if (current > version) {
  console.error(`The data has schema ${current}, and this version only knows up to ${version}`);
  process.exit(1);
}
writeFileSync(file, String(version));
console.log(`Example migration: schema ${current} to ${version}`);
