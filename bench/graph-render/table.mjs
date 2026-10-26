// results/*.jsonl -> the markdown results table used in docs/graph-render-research.md
// (a later line for the same renderer and dataset wins).
import fs from "node:fs";
const files = process.argv.slice(2);
const rows = new Map();
for (const f of files) for (const l of fs.readFileSync(f, "utf8").split("\n").filter(Boolean)) { const r = JSON.parse(l); if (r.fatal && !r.marks && rows.has(r.renderer + "|" + r.ds)) continue; rows.set(r.renderer + "|" + r.ds, r); }
const DS = [["synth2k", "Synthetic 2k (2,000 nodes / 4,683 edges)"], ["django-capped", "django default view (8,204 / 27,975)"], ["synth10k", "Synthetic 10k (10,000 / 23,362)"], ["django-uncapped", "django, ceiling released (10,691 / 44,660)"], ["synth50k", "Synthetic 50k (50,000 / 116,836)"]];
const RN = [["current", "Current page (Cytoscape 3.30 canvas)"], ["webgl", "Current page, Cytoscape 3.34 WebGL"], ["cyweb", "Cytoscape 3.34 WebGL + worker layout"], ["cycanvas", "Cytoscape 3.34 canvas + worker layout"], ["sigma", "sigma 4 + FA2 worker"], ["cosmos", "cosmos.gl"], ["d3canvas", "d3-force worker + Canvas 2D"], ["forcegraph", "force-graph"]];
const s = ms => ms == null ? "—" : ms >= 10000 ? (ms / 1000).toFixed(0) : (ms / 1000).toFixed(1);
const f = x => x == null ? "—" : x.fps < 10 ? x.fps.toFixed(1) : String(Math.round(x.fps));
console.log("| Dataset | Renderer | First frame (s) | Layout settled (s) | FPS while settling | Pan FPS | Zoom FPS | Node drag FPS | JS heap (MB) |");
console.log("|---|---|---:|---:|---:|---:|---:|---:|---:|");
for (const [d, dl] of DS) for (const [r, rl] of RN) {
  const x = rows.get(r + "|" + d); if (!x) continue;
  const m = x.marks || {};
  const first = x.firstOk ? s(m.first) : m.first ? s(m.first) : "> 240";
  const settle = x.settledOk ? s(m.settled - (m.data ?? 0)) : "> 240";
  const pan = x.pan ? f(x.pan) : x.panApi ? f(x.panApi) + "*" : "—";
  console.log(`| ${dl} | ${rl} | ${first} | ${settle} | ${x.settleFps == null ? "—" : x.settleFps < 10 ? x.settleFps.toFixed(1) : Math.round(x.settleFps)} | ${pan} | ${f(x.zoom)} | ${f(x.drag)} | ${x.jsHeapMB != null ? Math.round(x.jsHeapMB) : "—"} |`);
}
