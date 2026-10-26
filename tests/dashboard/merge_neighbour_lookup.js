/* Headless test that mergeGraphElements finds a new node's neighbour in time
   linear in the reply, lifted verbatim out of index.html.

   For every new node without a saved position, the merge looks for one
   incoming relationship that touches it, so the node can be placed next to
   its neighbour. That lookup used to scan the whole relationship list per
   node (Array.find): O(nodes x relationships), 5 s on an 8k-node view and
   108 s at 50k. The check counts how often the merge reads a relationship's
   endpoints, which a per-node scan multiplies by the node count. */
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.join(__dirname, "..", "..", "devgraph", "dashboard", "static", "index.html"), "utf8");
const grab = (startRe, endMarker) => {
  const i = html.search(startRe);
  if (i < 0) throw new Error("could not find " + startRe);
  const j = html.indexOf(endMarker, i);
  if (j < 0) throw new Error("could not find end marker after " + startRe);
  return html.slice(i, j + endMarker.length);
};
const src = grab(/^function mergeGraphElements\(/m, "\n}");

// --- a minimal canvas: graphView over a Map of nodes and edges ----------
const nodes = new Map(), edges = new Map();
const graphView = {
  has: id => nodes.has(id) || edges.has(id),
  hasNode: id => nodes.has(id),
  elementIds: () => [...nodes.keys(), ...edges.keys()],
  elementCount: () => nodes.size + edges.size,
  data: (id, k) => (nodes.get(id) || edges.get(id)).data[k],
  setData: (id, k, v) => { (nodes.get(id) || edges.get(id)).data[k] = v; },
  position: id => ({ ...nodes.get(id).pos }),
  forEachPosition: fn => nodes.forEach((n, id) => fn(id, n.pos.x, n.pos.y)),
  addNode: (id, data, p) => nodes.set(id, { data: { ...data }, pos: { ...p } }),
  addEdge: (id, source, target, data) => edges.set(id, { data: { ...data }, source, target }),
  remove: id => { nodes.delete(id); edges.delete(id); },
  size: () => ({ width: 900, height: 600 }),
};
const globals = {
  graphView, cachedPositions: new Map(),
  document: { getElementById: () => null },
  updateOnscreenCount: () => {},
  setGraphSettled: () => {}, forceDirectedSettle: () => {}, focusRotation: () => {},
};
const mergeGraphElements = new Function(...Object.keys(globals), src + "\nreturn mergeGraphElements;")(
  ...Object.values(globals));

let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};

// An anchored canvas (one node already placed) and a large batch of new
// nodes, each the far end of one relationship from the anchor's chain.
const N = 3000;
graphView.addNode("n0", { id: "n0" }, { x: 500, y: -200 });
let endpointReads = 0;
const counted = (id, source, target) => ({
  data: {
    id,
    get source() { endpointReads++; return source; },
    get target() { endpointReads++; return target; },
  },
});
const elements = [{ data: { id: "n0" } }];
for (let i = 1; i <= N; i++) elements.push({ data: { id: "n" + i } });
for (let i = 1; i <= N; i++) elements.push(counted("e" + i, "n" + (i - 1), "n" + i));

mergeGraphElements(elements, "light", { remove: false, settle: false });
check(`neighbour lookup reads each relationship a bounded number of times (${endpointReads} reads for ${N} nodes and ${N} relationships)`,
  endpointReads < 20 * (2 * N), String(endpointReads));
check("every new node and edge is on the canvas", nodes.size + edges.size === 2 * N + 1, String(nodes.size + edges.size));
const n1 = graphView.position("n1");
check("a new node is still placed next to its already-placed neighbour",
  Math.abs(n1.x - 500) <= 25 && Math.abs(n1.y + 200) <= 25, JSON.stringify(n1));

console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
process.exit(failures ? 1 : 0);
