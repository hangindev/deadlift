// The example App's Compose healthcheck: exit 0 when `/health` answers 200.
const response = await fetch("http://127.0.0.1:3000/health").catch(() => undefined);
process.exit(response?.ok ? 0 : 1);
