// Synthetic DevGraph canvas payloads, shaped exactly like POST /api/cypher
// {canvas:true} returns for the dashboard's state query
//   MATCH (n) WHERE n.repo_id = ... OPTIONAL MATCH (n)-[r]-(m) RETURN n, r, m, id(n) AS matchedId
// i.e. one row per (node, incident relationship) -- every relationship twice --
// or one row per orphan node. Mix of labels/relationship types follows a real
// indexed Python repo (django: ~70% Function, ~14% Class, ~13% Module; ~2.3
// relationships per node, CONTAINS tree + power-law CALLS within packages).
// Usage: node gen.mjs <nodes> <outPrefix> [seed]
import fs from "node:fs";

const N = +process.argv[2];
const out = process.argv[3];
let seed = +(process.argv[4] || 42);
const rnd = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296);
const pick = a => a[Math.floor(rnd() * a.length)];
const REPO = "zz-render-synth";
const SEP = "\u001f";

const nodes = []; // {id, label, name, file, owner}
const rels = [];  // {id, type, s, t}
let nid = 1000, rid = 500000;
const add = (label, name, file, owner) => { const n = { id: nid++, label, name, file, owner }; nodes.push(n); return n; };
const rel = (type, s, t) => rels.push({ id: rid++, type, s: s.id, t: t.id });

const repo = add("Repository", REPO, null, null);
const nMod = Math.max(3, Math.round(N * 0.13));
const nCls = Math.round(N * 0.14);
const nEp = Math.round(N * 0.008);
const nDs = Math.max(1, Math.round(N * 0.0015));
const nDoc = Math.round(N * 0.01);
const nFn = N - 1 - nMod - nCls - nEp - nDs - nDoc;

const pkgCount = Math.max(1, Math.round(nMod / 12));
const mods = [];
for (let i = 0; i < nMod; i++) {
  const pkg = i % pkgCount;
  const m = add("Module", `pkg${pkg}/mod${i}.py`, `pkg${pkg}/mod${i}.py`, null);
  m.pkg = pkg; mods.push(m); rel("CONTAINS", repo, m);
}
const classes = [];
for (let i = 0; i < nCls; i++) {
  const m = pick(mods);
  const c = add("Class", `Class${i}`, m.file, null); c.pkg = m.pkg; classes.push(c); rel("CONTAINS", m, c);
}
const fns = []; const fnsByPkg = new Map();
for (let i = 0; i < nFn; i++) {
  let owner;
  if (rnd() < 0.55 && classes.length) owner = pick(classes); else owner = pick(mods);
  const f = add("Function", `func_${i}`, owner.file, owner.label === "Class" ? owner.name : null);
  f.pkg = owner.pkg; f.w = 1; fns.push(f); rel("CONTAINS", owner, f);
  if (!fnsByPkg.has(f.pkg)) fnsByPkg.set(f.pkg, []);
  fnsByPkg.get(f.pkg).push(f);
}
// power-law CALLS: preferential attachment via a "popular" pool, 75% within package
const pool = [], poolByPkg = new Map();
fns.forEach(f => { pool.push(f); if (!poolByPkg.has(f.pkg)) poolByPkg.set(f.pkg, []); poolByPkg.get(f.pkg).push(f); });
const nCalls = Math.round(N * 1.15);
for (let i = 0; i < nCalls && fns.length > 1; i++) {
  const s = pick(fns);
  const local = rnd() < 0.75 ? poolByPkg.get(s.pkg) : pool;
  const t = pick(local);
  if (t === s) continue;
  rel("CALLS", s, t);
  local.push(t); if (local !== pool) pool.push(t);
}
for (let i = 0; i < Math.round(N * 0.1) && classes.length > 1; i++) { const a = pick(classes), b = pick(classes); if (a !== b) rel("EXTENDS", a, b); }
for (let i = 0; i < Math.round(N * 0.1); i++) { const a = pick(mods), b = rnd() < 0.6 ? pick(mods.filter(m => m.pkg === a.pkg)) : pick(mods); if (a !== b) rel("IMPORTS", a, b); }
for (let i = 0; i < nEp; i++) { const e = add("Endpoint", `/api/route${i}`, null, null); rel("IMPLEMENTS", pick(fns), e); }
for (let i = 0; i < nDs; i++) { const d = add(pick(["Database", "Queue", "Cache", "VectorStore"]), `store${i}`, null, null); for (let k = 0; k < 4; k++) rel(pick(["READS_FROM", "WRITES_TO"]), pick(fns), d); }
for (let i = 0; i < nDoc; i++) { const d = add("Document", `docs/page${i}.md`, `docs/page${i}.md`, null); rel("MENTIONS", d, pick(rnd() < 0.5 ? classes : fns)); }

const byId = new Map(nodes.map(n => [n.id, n]));
const keyOf = n => [n.label, REPO, n.name, n.file || "", n.owner || ""].join(SEP);
const nodeJson = n => ({ id: n.id, labels: [n.label], properties: Object.assign({ name: n.name, repo_id: REPO }, n.file ? { file: n.file } : {}, n.owner ? { owner: n.owner } : {}), key: keyOf(n) });
const incident = new Map(nodes.map(n => [n.id, []]));
rels.forEach(r => { incident.get(r.s).push(r); incident.get(r.t).push(r); });

// API-shaped payload (what the dashboard's own page fetches)
const data = [];
nodes.forEach(n => {
  const inc = incident.get(n.id);
  if (!inc.length) { data.push({ row: [null, null, null, n.id], graph: { nodes: [nodeJson(n)], relationships: [] } }); return; }
  inc.forEach(r => {
    const m = byId.get(r.s === n.id ? r.t : r.s);
    data.push({ row: [null, null, null, n.id], graph: { nodes: [nodeJson(n), nodeJson(m)], relationships: [{ id: r.id, type: r.type, startNode: r.s, endNode: r.t, properties: {} }] } });
  });
});
const api = { results: [{ columns: ["n", "r", "m", "matchedId"], data }], errors: [] };
fs.writeFileSync(`${out}.api.json`, JSON.stringify(api));

// compact form fed to the isolated renderer harnesses: exactly what the page's
// mapGraphResultToElements produces (stable ids, label, category, type)
const CAT = { Repository: "repo", Container: "repo", Service: "service", Module: "module", Class: "class", Function: "function", Endpoint: "endpoint", Database: "database", VectorStore: "vectorstore", Queue: "queue", Cache: "cache", Document: "phase2" };
const sid = n => "live:" + keyOf(n);
const compact = {
  nodes: nodes.map(n => ({ id: sid(n), label: n.name, cat: CAT[n.label] || "phase2", ntype: n.label })),
  edges: rels.map(r => ({ id: "live-e:" + r.id, source: sid(byId.get(r.s)), target: sid(byId.get(r.t)), label: r.type })),
};
fs.writeFileSync(`${out}.compact.json`, JSON.stringify(compact));
const labelCounts = {}; nodes.forEach(n => labelCounts[n.label] = (labelCounts[n.label] || 0) + 1);
const relCounts = {}; rels.forEach(r => relCounts[r.type] = (relCounts[r.type] || 0) + 1);
fs.writeFileSync(`${out}.meta.json`, JSON.stringify({ repo: REPO, nodes: nodes.length, edges: rels.length, rows: data.length, labelCounts, relCounts }));
console.log(out, "nodes", nodes.length, "edges", rels.length, "rows", data.length, "apiMB", (fs.statSync(`${out}.api.json`).size / 1e6).toFixed(1), "compactMB", (fs.statSync(`${out}.compact.json`).size / 1e6).toFixed(1));
