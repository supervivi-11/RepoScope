import { createReadStream, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize, resolve } from "node:path";

const host = process.env.REPOSCOPE_PREVIEW_HOST ?? "127.0.0.1";
const port = Number(process.env.REPOSCOPE_PREVIEW_PORT ?? "4173");
const root = resolve(process.env.REPOSCOPE_PREVIEW_DIR ?? "dist");
const contentTypes = new Map([
  [".css", "text/css; charset=utf-8"],
  [".html", "text/html; charset=utf-8"],
  [".js", "text/javascript; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
  [".svg", "image/svg+xml"],
]);

function safeFile(url) {
  const pathname = new URL(url, `http://${host}:${port}`).pathname;
  const relative = normalize(decodeURIComponent(pathname)).replace(/^[/\\]+/, "");
  const candidate = resolve(join(root, relative || "index.html"));
  if (candidate !== root && !candidate.startsWith(`${root}\\`) && !candidate.startsWith(`${root}/`)) return null;
  try {
    return statSync(candidate).isFile() ? candidate : join(root, "index.html");
  } catch {
    return join(root, "index.html");
  }
}

const server = createServer((request, response) => {
  const file = safeFile(request.url ?? "/");
  if (file === null) {
    response.writeHead(400).end("Bad request");
    return;
  }
  response.setHeader("Content-Type", contentTypes.get(extname(file)) ?? "application/octet-stream");
  response.setHeader("Cache-Control", "no-store");
  createReadStream(file).on("error", () => response.writeHead(404).end("Not found")).pipe(response);
});

server.listen(port, host);
