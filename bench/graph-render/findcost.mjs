// Cost of mergeGraphElements' per-new-node neighbour lookup (Array.find over all incoming edges) vs a prebuilt map.
import fs from "node:fs";
for (const ds of process.argv.slice(2)) {
  const d = JSON.parse(fs.readFileSync(`data/${ds}.compact.json`));
  const edges = d.edges.map(e => ({ data: { source: e.source, target: e.target } }));
  let t = performance.now(), hit = 0;
  for (const n of d.nodes) { const link = edges.find(e => e.data.source === n.id || e.data.target === n.id); if (link) hit++; }
  const findMs = performance.now() - t;
  t = performance.now(); const first = new Map();
  for (const e of edges) { if (!first.has(e.data.source)) first.set(e.data.source, e); if (!first.has(e.data.target)) first.set(e.data.target, e); }
  for (const n of d.nodes) first.get(n.id);
  console.log(ds, "nodes", d.nodes.length, "edges", edges.length, "find", findMs.toFixed(0), "ms", "map", (performance.now() - t).toFixed(0), "ms");
}
