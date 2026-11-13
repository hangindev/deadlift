// The example App's web server, on port 3000: `/health` passes unless the image was built with
// HEALTH=fail, `/version` names the image, so a test can tell which version is live, and
// `/env/<NAME>` answers the container's variable NAME, 404 when it has none. `/note` keeps a note
// in the App's named volume: PUT one, then GET it back, so a test can tell whether data written
// earlier is still there.
import { readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";

const healthy = process.env.EXAMPLE_HEALTH !== "fail";
const version = process.env.EXAMPLE_VERSION ?? "";

const server = createServer((request, response) => {
  if (request.url === "/health") {
    response.writeHead(healthy ? 200 : 503).end(healthy ? "ok" : "failing on purpose");
  } else if (request.url === "/version") {
    response.end(version);
  } else if (request.url === "/note" && request.method === "PUT") {
    let body = "";
    request.on("data", (chunk) => (body += chunk));
    request.on("end", () => {
      writeFileSync("/data/note", body);
      response.end();
    });
  } else if (request.url === "/note") {
    let note = "";
    try {
      note = readFileSync("/data/note", "utf8");
    } catch {
      // No note yet.
    }
    response.end(note);
  } else if (request.url?.startsWith("/env/")) {
    const value = process.env[request.url.slice("/env/".length)];
    if (value === undefined) response.writeHead(404).end();
    else response.end(value);
  } else {
    response.writeHead(404).end();
  }
});
server.listen(3000, "0.0.0.0");

// As PID 1, Node has no default handler, so without this Docker would wait and then kill it.
process.on("SIGTERM", () => process.exit(0));
