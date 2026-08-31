import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const marker = "STATIC_CREDENTIAL_MARKER_7f91";
const root = mkdtempSync(join(tmpdir(), "reposcope-build-config-"));
const vite = resolve("node_modules/vite/bin/vite.js");

function run(mode, outDir, env) {
  return spawnSync(
    process.execPath,
    [vite, "build", "--mode", mode, "--outDir", outDir, "--emptyOutDir"],
    { cwd: process.cwd(), env: { ...process.env, ...env }, encoding: "utf8" },
  );
}

function files(path) {
  return readdirSync(path, { withFileTypes: true }).flatMap((entry) => {
    const child = join(path, entry.name);
    return entry.isDirectory() ? files(child) : [child];
  });
}

try {
  const staticOut = join(root, "static");
  const staticBuild = run("production", staticOut, {
    VITE_REPOSCOPE_MODE: "static",
    VITE_API_BASE_URL: `https://user:${marker}@unsafe.example.test/api`,
  });
  if (staticBuild.status !== 0) {
    throw new Error("Static build verification failed.");
  }
  const bundle = files(staticOut).map((file) => readFileSync(file, "utf8")).join("\n");
  if (bundle.includes(marker) || bundle.includes("unsafe.example.test")) {
    throw new Error("Static bundle retained live API configuration.");
  }

  const liveBuild = run("live", join(root, "live"), {
    VITE_REPOSCOPE_MODE: "live",
    VITE_API_BASE_URL: `https://user:${marker}@unsafe.example.test/api`,
  });
  if (liveBuild.status === 0) {
    throw new Error("Unsafe live build unexpectedly succeeded.");
  }
  const output = `${liveBuild.stdout}\n${liveBuild.stderr}`;
  if (!output.includes("Unsafe VITE_API_BASE_URL") || output.includes(marker)) {
    throw new Error("Unsafe live build did not fail with a sanitized error.");
  }
  process.stdout.write("build configuration isolation verified\n");
} finally {
  rmSync(root, { recursive: true, force: true });
}
