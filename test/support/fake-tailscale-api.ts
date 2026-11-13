// A fake Tailscale API, for `deadlift app add` and `app remove`: the Services endpoints only,
// on the host's loopback, so no test reaches the real API or needs a real credential. Point
// deadlift at it with DEADLIFT_TAILSCALE_API.
import { createServer, type IncomingMessage } from "node:http";

export type Service = { name: string; comment?: string; ports: string[]; tags: string[] };

export type ApiRequest = { method: string; path: string; authorization?: string; body?: unknown };

/** The credential the fake accepts, as TAILSCALE_API_KEY. */
export const fakeApiKey = "tskey-api-fake";

async function readBody(request: IncomingMessage): Promise<string> {
  let body = "";
  for await (const chunk of request) body += chunk;
  return body;
}

export async function startFakeTailscaleApi() {
  const services = new Map<string, Service>();
  const requests: ApiRequest[] = [];

  const server = createServer(async (request, response) => {
    const text = await readBody(request);
    const path = decodeURIComponent(new URL(request.url ?? "/", "http://fake").pathname);
    requests.push({
      method: request.method ?? "",
      path,
      authorization: request.headers.authorization,
      ...(text ? { body: JSON.parse(text) as unknown } : {}),
    });
    const reply = (status: number, body: unknown = {}) => {
      response.writeHead(status, { "Content-Type": "application/json" });
      response.end(JSON.stringify(body));
    };
    if (request.headers.authorization !== `Bearer ${fakeApiKey}`) return reply(401, { message: "API token invalid" });
    const match = /^\/api\/v2\/tailnet\/-\/vip-services\/(svc:[a-z0-9-]+)$/.exec(path);
    if (!match) return reply(404, { message: "not found" });
    const name = match[1] ?? "";
    switch (request.method) {
      case "GET": {
        const service = services.get(name);
        return service ? reply(200, { ...service, addrs: ["100.100.0.1"] }) : reply(404, { message: "not found" });
      }
      case "PUT": {
        const service = JSON.parse(text) as Service;
        if (service.name !== name) return reply(400, { message: "name mismatch" });
        services.set(name, service);
        return reply(200, { ...service, addrs: ["100.100.0.1"] });
      }
      case "DELETE":
        if (!services.delete(name)) return reply(404, { message: "not found" });
        return reply(200);
      default:
        return reply(405, { message: "method not allowed" });
    }
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (typeof address !== "object" || address === null) throw new Error("no address");

  return {
    url: `http://127.0.0.1:${address.port}`,
    /** The Services as the tailnet has them now, by name. */
    services,
    /** Every request so far, in order. */
    requests,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}
