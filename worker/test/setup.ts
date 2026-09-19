import { env, applyD1Migrations } from "cloudflare:test";
import { beforeAll } from "vitest";

declare module "cloudflare:test" {
  interface ProvidedEnv extends Env {
    TEST_MIGRATIONS: D1Migration[];
  }
}

// Without this the state table doesn't exist, every handler 500s, and an auth
// test asserting "not 401" passes on the error — which is exactly what happened.
beforeAll(async () => {
  await applyD1Migrations(env.STATE_DB, env.TEST_MIGRATIONS);
});
