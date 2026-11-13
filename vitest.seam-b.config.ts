import { configDefaults, defineConfig } from "vitest/config";

// Seam B: bootstraps the machine it runs on into a Server, so it runs only on a throwaway machine
// (the tests refuse to start anywhere else), by `pnpm test:seam-b`, never by `pnpm test`.
export default defineConfig({
  test: {
    include: ["test/seam-b/**/*.test.ts"],
    exclude: [...configDefaults.exclude, ".claude/**"],
    fileParallelism: false,
    testTimeout: 600_000,
    hookTimeout: 600_000,
  },
});
