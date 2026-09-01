import { spawn, spawnSync } from "node:child_process";
import { resolve } from "node:path";

const host = "127.0.0.1";
const port = "4173";
const server = spawn(process.execPath, [resolve("scripts/serve-dist.mjs")], {
  cwd: process.cwd(),
  env: { ...process.env, REPOSCOPE_PREVIEW_HOST: host, REPOSCOPE_PREVIEW_PORT: port },
  stdio: "inherit",
});

function delay(milliseconds) {
  return new Promise((resolveDelay) => setTimeout(resolveDelay, milliseconds));
}

async function waitUntilReady() {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (server.exitCode !== null) throw new Error(`Preview server exited with code ${server.exitCode}.`);
    try {
      const response = await fetch(`http://${host}:${port}/`);
      if (response.ok) return;
    } catch {
      // The server may still be binding the loopback socket.
    }
    await delay(50);
  }
  throw new Error("Preview server did not become ready.");
}

async function stopServer() {
  if (server.exitCode !== null || server.pid === undefined) return;
  server.kill();
  await Promise.race([
    new Promise((resolveExit) => server.once("exit", resolveExit)),
    delay(2_000),
  ]);
  if (server.exitCode === null && process.platform === "win32") {
    spawnSync("taskkill", ["/pid", String(server.pid), "/T", "/F"], { stdio: "ignore" });
  } else if (server.exitCode === null) {
    server.kill("SIGKILL");
  }
}

let exitCode = 1;
try {
  await waitUntilReady();
  exitCode = await new Promise((resolveExit, reject) => {
    const playwright = spawn(
      process.execPath,
      [resolve("node_modules/@playwright/test/cli.js"), "test", ...process.argv.slice(2)],
      { cwd: process.cwd(), env: { ...process.env, REPOSCOPE_EXTERNAL_SERVER: "1" }, stdio: "inherit" },
    );
    playwright.once("error", reject);
    playwright.once("exit", (code) => resolveExit(code ?? 1));
  });
} finally {
  await stopServer();
}
process.exitCode = exitCode;
