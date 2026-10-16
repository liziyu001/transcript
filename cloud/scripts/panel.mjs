// Static control panel only. No credentials, scraping, or cloud work runs in this server.
import http from "node:http";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
const files = new Map([
  ["/", ["index.html", "text/html"]],
  ["/static/app.js", ["app.js", "text/javascript"]],
  ["/static/styles.css", ["styles.css", "text/css"]],
]);
http.createServer(async (req, res) => {
  const file = files.get(new URL(req.url, "http://localhost").pathname);
  if (!file) { res.writeHead(404).end(); return; }
  try {
    const data = await readFile(fileURLToPath(new URL("../src/transcript_cloud/static/" + file[0], import.meta.url)));
    res.writeHead(200, { "Content-Type": file[1] + "; charset=utf-8", "Cache-Control": "no-store" }).end(data);
  } catch { res.writeHead(500).end("Could not load panel"); }
}).listen(8787, "127.0.0.1", () => console.log("Control panel: http://localhost:8787"));
