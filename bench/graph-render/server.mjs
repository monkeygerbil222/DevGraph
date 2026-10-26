// Mock DevGraph dashboard server for the render benchmark.
// Serves the worktree's real dashboard page (devgraph/dashboard/static) and
// answers just enough of /api/* for its boot path, with the graph query
// answered from a pre-generated API-shaped payload. Harness pages and the
// candidate libraries are served from /bench/.
//   node server.mjs <port> [checkout root, default ../..]
// Dataset: GET /bench/ds/<name> selects data/<name>.api.json for /api/cypher.
// Variants of the pre-rework (Cytoscape) page: /?variant=v334 (Cytoscape 3.34, canvas),
//   /?variant=webgl (Cytoscape 3.34 with its WebGL renderer on).
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import url from "node:url";

const port = +process.argv[2] || 8899;
const here = path.dirname(url.fileURLToPath(import.meta.url));
const worktree = process.argv[3] || path.resolve(here, "../..");
let ds = "synth2k", apiBody = null, meta = null;
function loadDs(name) {
  ds = name;
  apiBody = fs.readFileSync(path.join(here, "data", `${name}.api.json`));
  meta = JSON.parse(fs.readFileSync(path.join(here, "data", `${name}.meta.json`)));
}
loadDs(ds);

const TYPES = { ".html": "text/html", ".js": "text/javascript", ".mjs": "text/javascript", ".json": "application/json", ".css": "text/css", ".map": "application/json" };
function sendFile(res, file) {
  if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); return res.end(); }
  res.writeHead(200, { "Content-Type": TYPES[path.extname(file)] || "application/octet-stream", "Cache-Control": "no-store" });
  fs.createReadStream(file).pipe(res);
}
const json = (res, obj) => { res.writeHead(200, { "Content-Type": "application/json" }); res.end(typeof obj === "string" || Buffer.isBuffer(obj) ? obj : JSON.stringify(obj)); };
const table = (columns, rows) => ({ results: [{ columns, data: rows.map(row => ({ row, graph: { nodes: [], relationships: [] } })) }], errors: [] });

function pageHtml(variant) {
  let html = fs.readFileSync(path.join(worktree, "devgraph/dashboard/static/index.html"), "utf8");
  if (variant === "v334" || variant === "webgl") {
    html = html.replace('/static/vendor/cytoscape-3.30.2.min.js', '/bench/node_modules/cytoscape/dist/cytoscape.min.js');
  }
  if (variant === "webgl") {
    html = html.replace("  hideEdgesOnViewport: false,\n});", "  hideEdgesOnViewport: false,\n  webgl: true, webglBgColor: [8, 9, 10],\n});");
  }
  return html;
}

http.createServer((req, res) => {
  const u = new URL(req.url, "http://x");
  const p = u.pathname;
  if (p === "/" ) { res.writeHead(200, { "Content-Type": "text/html", "Cache-Control": "no-store" }); return res.end(pageHtml(u.searchParams.get("variant"))); }
  if (p.startsWith("/static/")) return sendFile(res, path.join(worktree, "devgraph/dashboard", p));
  if (p.startsWith("/bench/ds/")) { loadDs(p.slice(10)); return json(res, { ds, meta }); }
  if (p.startsWith("/bench/")) return sendFile(res, path.join(here, p.slice(7)));
  if (p === "/api/events") { res.writeHead(200, { "Content-Type": "text/event-stream" }); res.write(":\n\n"); return; }
  if (p === "/api/repos") return json(res, { repos: [{ repo_id: meta.repo, active: true, path: "/tmp/x" }] });
  if (/^\/api\/repos\/[^/]+\/schema$/.test(p)) return json(res, { node_types: [], relationship_types: [], schema_state: "ok", hidden_properties: [] });
  if (/^\/api\/repos\/[^/]+\/layout$/.test(p)) { if (req.method === "PUT") { req.resume(); return json(res, { ok: true }); } return json(res, {}); }
  if (p === "/api/cypher") {
    let body = "";
    req.on("data", c => body += c);
    req.on("end", () => {
      const q = JSON.parse(body).query || "";
      if (/RETURN n, r, m, id\(n\) AS matchedId/.test(q)) return json(res, apiBody);
      if (/RETURN labels\(n\)\[0\] AS label, count\(\*\)/.test(q)) return json(res, table(["label", "n"], Object.entries(meta.labelCounts)));
      if (/RETURN type\(r\) AS t, count\(\*\)/.test(q)) return json(res, table(["t", "n"], Object.entries(meta.relCounts)));
      if (/RETURN count\(r\) AS n/.test(q)) return json(res, table(["n"], [[meta.rows]]));
      if (/RETURN 1 AS ok/.test(q)) return json(res, table(["ok"], [[1]]));
      return json(res, table([], []));
    });
    return;
  }
  return json(res, {});
}).listen(port, "127.0.0.1", () => console.log("mock on", port));
