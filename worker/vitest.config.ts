import { defineConfig } from "vitest/config";
import { cloudflareTest, readD1Migrations } from "@cloudflare/vitest-pool-workers";

// Read at config time (Node), handed to the tests as a binding they apply.
const migrations = await readD1Migrations("./migrations");

export default defineConfig({
  test: { setupFiles: ["./test/setup.ts"] },
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.jsonc" },
      miniflare: {
        // Stand-ins for the secrets. The real ones live in `wrangler secret`.
        bindings: {
          DASHBOARD_PASSPHRASE: "correct-horse-battery-staple",
          BOT_TOKEN: "test-bot-token",
          TEST_MIGRATIONS: migrations,
        },
      },
    }),
  ],
});
