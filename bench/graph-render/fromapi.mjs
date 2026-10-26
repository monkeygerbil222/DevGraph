// Turns an exported real /api/cypher canvas payload into the compact form and
// meta the harness uses, with the same mapping as mapGraphResultToElements.
import fs from "node:fs";
const name = process.argv[2];
const api = JSON.parse(fs.readFileSync(`data/${name}.api.json`));
const CAT = { Repository: "repo", Container: "repo", Service: "service", Module: "module", Class: "class", Function: "function", Endpoint: "endpoint", Database: "database", VectorStore: "vectorstore", Queue: "queue", Cache: "cache" };
const nodes = new Map(), edges = new Map(), idByInternal = new Map();
const r0 = api.results[0];
r0.data.forEach(r => {
  (r.graph?.nodes || []).forEach(n => { const sid = n.key != null ? "live:" + n.key : "live-anon:" + n.id; idByInternal.set(n.id, sid); if (!nodes.has(sid)) nodes.set(sid, { id: sid, label: n.properties?.name || n.labels[0], cat: CAT[n.labels[0]] || "phase2", ntype: n.labels[0] }); });
  (r.graph?.relationships || []).forEach(rel => { const s = idByInternal.get(rel.startNode), t = idByInternal.get(rel.endNode); if (s && t) edges.set("live-e:" + rel.id, { id: "live-e:" + rel.id, source: s, target: t, label: rel.type }); });
});
const compact = { nodes: [...nodes.values()], edges: [...edges.values()] };
fs.writeFileSync(`data/${name}.compact.json`, JSON.stringify(compact));
const labelCounts = {}; compact.nodes.forEach(n => labelCounts[n.ntype] = (labelCounts[n.ntype] || 0) + 1);
const relCounts = {}; compact.edges.forEach(e => relCounts[e.label] = (relCounts[e.label] || 0) + 1);
fs.writeFileSync(`data/${name}.meta.json`, JSON.stringify({ repo: "zz-render-django", nodes: compact.nodes.length, edges: compact.edges.length, rows: r0.data.length, truncated: r0.truncated ?? null, labelCounts, relCounts }));
console.log(name, "rows", r0.data.length, "truncated", JSON.stringify(r0.truncated ?? Object.keys(r0).filter(k=>k!=="data")), "nodes", compact.nodes.length, "edges", compact.edges.length, labelCounts);
