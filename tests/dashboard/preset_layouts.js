/* Headless test of the Layout menu's non-force layouts (presetLayout, lifted
   verbatim out of index.html), which replaced Cytoscape's breadthfirst,
   concentric and grid layouts: every node gets its own finite position,
   centred on the origin, with each layout's shape. */
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.join(__dirname, "..", "..", "devgraph", "dashboard", "static", "index.html"), "utf8")
  .replace(/\r\n/g, "\n"); // a Windows checkout has CRLF line endings; the extractors match on \n
const grab = (startRe, endMarker) => {
  const i = html.search(startRe);
  if (i < 0) throw new Error("could not find " + startRe);
  const j = html.indexOf(endMarker, i);
  if (j < 0) throw new Error("could not find end marker after " + startRe);
  return html.slice(i, j + endMarker.length);
};
const src = [grab(/^const PRESET_LAYOUT_SPACING = /m, "\n"), grab(/^function presetLayout\(/m, "\n}")].join("\n");
const presetLayout = new Function(src + "\nreturn presetLayout;")();

let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};

// a hub with a tree under it, a separate pair, an island and a self-loop
const ids = ["hub", "a", "b", "c", "d", "e", "p", "q", "island"];
const edges = [["hub", "a"], ["hub", "b"], ["hub", "c"], ["a", "d"], ["b", "e"], ["p", "q"], ["c", "c"]];
for (const name of ["breadthfirst", "concentric", "grid"]) {
  const out = presetLayout(name, ids, edges);
  const pts = ids.map(id => out[id]);
  check(`${name}: every node is placed`, pts.every(p => p && Number.isFinite(p.x) && Number.isFinite(p.y)), JSON.stringify(out));
  check(`${name}: no two nodes share a spot`, new Set(pts.map(p => p.x.toFixed(3) + "," + p.y.toFixed(3))).size === ids.length, JSON.stringify(out));
  const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
  check(`${name}: centred on the origin`,
    Math.abs((Math.min(...xs) + Math.max(...xs)) / 2) < 1e-9 + 40 && Math.abs((Math.min(...ys) + Math.max(...ys)) / 2) < 1e-9 + 40, JSON.stringify(out));
}
const bf = presetLayout("breadthfirst", ids, edges);
check("breadthfirst: the best-connected node heads its tree, its children a level below, theirs below that",
  bf.hub.y < bf.a.y && bf.a.y === bf.b.y && bf.b.y < bf.d.y, JSON.stringify(bf));
const cc = presetLayout("concentric", ids, edges);
const r = id => Math.hypot(cc[id].x, cc[id].y);
check("concentric: the best-connected node sits in the middle, leaves further out", r("hub") < r("d") && r("hub") <= r("a"), JSON.stringify(cc));
const gr = presetLayout("grid", ids, edges);
check("grid: rows in node order", gr.hub.y === gr.a.y && gr.a.x > gr.hub.x, JSON.stringify(gr));

console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
process.exit(failures ? 1 : 0);
