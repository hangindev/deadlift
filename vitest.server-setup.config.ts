import { configDefaults, defineConfig } from "vitest/config";

// The server-setup tests bootstrap the machine they run on into a Server, so they run only on a
// throwaway machine (the tests refuse to start anywhere else), by `pnpm test:server-setup`, never
// by `pnpm test`.
export default defineConfig({
  test: {
    include: ["test/server-setup/**/*.test.ts"],
    exclude: [...configDefaults.exclude, ".claude/**"],
    fileParallelism: false,
    testTimeout: 600_000,
    hookTimeout: 600_000,
  },
});
