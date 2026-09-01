import { resolveConfig } from "vite";
import { expect, test } from "vitest";

import { validateLiveApiBaseUrl } from "./vite.config";

test("the live development server resolves a same-origin /api proxy to the local backend", async () => {
  const config = await resolveConfig({ configFile: "vite.config.ts", mode: "live" }, "serve");
  const proxy = config.server.proxy;

  expect(proxy && typeof proxy === "object" ? proxy["/api"] : undefined).toMatchObject({
    target: "http://127.0.0.1:8000",
    changeOrigin: false,
  });
});

test.each([
  "https://user:password@api.example.test",
  "https://api.example.test/path?token=secret",
  "https://api.example.test/path#fragment",
])("rejects unsafe live build API configuration: %s", (value) => {
  expect(() => validateLiveApiBaseUrl(value)).toThrow("Unsafe VITE_API_BASE_URL");
});
