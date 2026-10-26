// Shared harness plumbing for the isolated renderer pages. Each page sets
// __B.pan(dx,dy), __B.zoomTo(level) / __B.zoom(), __B.nodeScreen(i) (for
// drag), and calls mark("first") on its first frame with every node drawn
// and mark("settled") when its layout stops.
const CAT_COLORS = {
  repo: "#b7b3ff", service: "#7c8dff", module: "#22c3e6", class: "#22c55e", function: "#a3e635",
  endpoint: "#fbbf24", database: "#f0813c", vectorstore: "#dc2626", queue: "#e0559e", cache: "#c084fc",
  phase2: "#9299a6", phase3: "#6b7280",
};
const catColor = c => CAT_COLORS[c] || "#8a8f98";
const STRUCTURAL_CATS = new Set(["repo", "service", "database", "vectorstore", "queue", "cache", "endpoint"]);
const nodeSize = c => STRUCTURAL_CATS.has(c) ? 15 : 9;
const hexRgb = h => { const n = parseInt(h.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; };
const BG = "#08090a";

window.__B = { marks: {}, info: {} };
function mark(k) { if (__B.marks[k] == null) __B.marks[k] = performance.now(); }
const params = new URLSearchParams(location.search);
async function loadData() {
  const res = await fetch(`/bench/data/${params.get("ds")}.compact.json`);
  const j = await res.json();
  mark("data");
  return j;
}
// Generic settle detector for layouts with no stop signal of their own:
// settled once the mean per-node movement over 500 ms stays under 0.03% of
// the layout's bounding-box diagonal for four samples in a row (cap 120 s).
function settleSampler(getXY, n, onSettled) {
  let prev = null, quiet = 0; const start = performance.now();
  const iv = setInterval(() => {
    const xy = getXY(); if (!xy) return;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (let i = 0; i < n; i++) { const x = xy[2 * i], y = xy[2 * i + 1]; if (x < minX) minX = x; if (x > maxX) maxX = x; if (y < minY) minY = y; if (y > maxY) maxY = y; }
    const diag = Math.hypot(maxX - minX, maxY - minY) || 1;
    if (prev) {
      let d = 0; for (let i = 0; i < n; i++) d += Math.hypot(xy[2 * i] - prev[2 * i], xy[2 * i + 1] - prev[2 * i + 1]);
      const rel = d / n / diag;
      quiet = rel < 0.0003 ? quiet + 1 : 0;
    }
    prev = Float32Array.from(xy);
    if (quiet >= 4 || performance.now() - start > 120000) { clearInterval(iv); __B.info.settleTimedOut = quiet < 4; onSettled(); }
  }, 500);
}
