import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export function validateLiveApiBaseUrl(value: string): string {
  const trimmed = value.trim();
  if (trimmed === "") return "";
  if (/[\u0000-\u0020\u007f]/.test(trimmed)) throw new Error("Unsafe VITE_API_BASE_URL");
  if (trimmed.startsWith("/")) {
    if (trimmed.startsWith("//") || trimmed.includes("?") || trimmed.includes("#") || trimmed.includes("\\")) {
      throw new Error("Unsafe VITE_API_BASE_URL");
    }
    return trimmed.replace(/\/+$/, "");
  }
  let parsed: URL;
  try { parsed = new URL(trimmed); } catch { throw new Error("Unsafe VITE_API_BASE_URL"); }
  if (!(["http:", "https:"].includes(parsed.protocol)) || parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error("Unsafe VITE_API_BASE_URL");
  }
  return `${parsed.origin}${parsed.pathname}`.replace(/\/+$/, "");
}

export default defineConfig(({ mode }) => {
  const environment = loadEnv(mode, process.cwd(), "");
  const live = mode === "live" || environment.VITE_REPOSCOPE_MODE === "live";
  const apiBaseUrl = live ? validateLiveApiBaseUrl(environment.VITE_API_BASE_URL ?? "") : "";
  return {
    plugins: [react()],
    define: {
      __REPOSCOPE_BUILD_MODE__: JSON.stringify(live ? "live" : "static"),
      __REPOSCOPE_API_BASE_URL__: JSON.stringify(apiBaseUrl),
    },
    server: live ? {
      proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } },
    } : undefined,
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: "./src/test/setup.ts",
      exclude: ["e2e/**", "node_modules/**", "dist/**"],
    },
  };
});
