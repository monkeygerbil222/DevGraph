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

// --- a minimal canvas: elements with ids, data and positions ------------
const live = new Map();
const mkNode = el => {
  const data = { ...el.data }, pos = { x: 0, y: 0 };
  return {
    id: () => data.id, length: 1, nonempty: () => true,
    data: (k, v) => (v === undefined ? data[k] : (data[k] = v)),
    position: p => (p === undefined ? { ...pos } : Object.assign(pos, p)),
  };
};
const coll = arr => ({
  length: arr.length,
  forEach: fn => arr.forEach(fn), map: fn => arr.map(fn), some: fn => arr.some(fn),
  filter: fn => coll(arr.filter(fn)), remove: () => {},
  boundingBox: () => {
    const xs = arr.map(n => n.position().x), ys = arr.map(n => n.position().y);
    const x1 = Math.min(...xs), x2 = Math.max(...xs), y1 = Math.min(...ys), y2 = Math.max(...ys);
    return { x1, x2, y1, y2, w: x2 - x1, h: y2 - y1 };
  },
});
const cy = {
  elements: () => coll([...live.values()]),
  nodes: () => coll([...live.values()].filter(e => !e.isEdge)),
  collection: () => coll([]),
  getElementById: id => live.get(id) || { length: 0, nonempty: () => false },
  add: els => els.forEach(el => { const e = mkNode(el); e.isEdge = !!el.data.source; live.set(el.data.id, e); }),
  container: () => ({ getBoundingClientRect: () => ({ width: 900, height: 600 }) }),
};
const globals = {
  cy, cachedPositions: new Map(),
  document: { getElementById: () => null },
  updateRenderQuality: () => {}, updateViewportCulling: () => {},
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
cy.add([{ data: { id: "n0" } }]);
live.get("n0").position({ x: 500, y: -200 });
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
check("every new node is on the canvas", live.size === 2 * N + 1, String(live.size));
const n1 = live.get("n1").position();
check("a new node is still placed next to its already-placed neighbour",
  Math.abs(n1.x - 500) <= 25 && Math.abs(n1.y + 200) <= 25, JSON.stringify(n1));

console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
process.exit(failures ? 1 : 0);
