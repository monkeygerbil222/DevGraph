/* Headless test of the canvas's force-directed settle, which runs in a Web
   Worker (devgraph/dashboard/static/graph-layout-worker.js).

   The settle moved off the main thread and onto typed arrays so a large
   graph settles without freezing the page, but its physics must not change:
   the same constants, order of operations and stopping rule keep the
   picture's shape. The reference below is the main-thread implementation
   the dashboard shipped before the move, verbatim apart from being
   unwrapped from Cytoscape (positions in a Map, edges as id pairs); the
   worker must reproduce it step for step. The worker's message loop is
   driven too: it posts positions, pauses, resumes, stops, and a new settle
   supersedes the old one. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const workerSrc = fs.readFileSync(
  path.join(__dirname, "..", "..", "devgraph", "dashboard", "static", "graph-layout-worker.js"), "utf8");

// --- the reference: the pre-worker main-thread settle -------------------
const QUAD_THETA = 0.8;
function makeQuadNode(cx, cy, half) {
  return { cx, cy, half, mass: 0, comX: 0, comY: 0, children: null, leafId: null };
}
function quadChildFor(node, p) {
  const right = p.x >= node.cx, bottom = p.y >= node.cy;
  return node.children[(bottom ? 2 : 0) + (right ? 1 : 0)];
}
function quadInsert(node, id, p, depth) {
  if (node.mass === 0 && node.children === null) {
    node.mass = 1; node.comX = p.x; node.comY = p.y; node.leafId = id;
    return;
  }
  if (node.children === null) {
    // was a leaf -- split into 4 quadrants and re-insert the point it held.
    // Depth-capped: two nodes at (near-)identical positions would otherwise
    // recurse forever trying to separate them into ever-smaller quadrants.
    // Past depth 24 just let the cell hold multiple points (comX/comY
    // becomes their shared centroid) rather than splitting further --
    // negligible accuracy cost for what's already an approximation.
    if (depth >= 24) { node.leafId = null; }
    else {
      const oldId = node.leafId, oldP = { x: node.comX, y: node.comY };
      node.leafId = null;
      const h = node.half / 2;
      node.children = [
        makeQuadNode(node.cx - h, node.cy - h, h),
        makeQuadNode(node.cx + h, node.cy - h, h),
        makeQuadNode(node.cx - h, node.cy + h, h),
        makeQuadNode(node.cx + h, node.cy + h, h),
      ];
      quadInsert(quadChildFor(node, oldP), oldId, oldP, depth + 1);
    }
  }
  if (node.children !== null) quadInsert(quadChildFor(node, p), id, p, depth + 1);
  node.comX = (node.comX * node.mass + p.x) / (node.mass + 1);
  node.comY = (node.comY * node.mass + p.y) / (node.mass + 1);
  node.mass += 1;
}
function buildQuadTree(ids, positions) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  ids.forEach(id => {
    const p = positions.get(id);
    if (p.x < minX) minX = p.x; if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y; if (p.y > maxY) maxY = p.y;
  });
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  const half = Math.max(maxX - minX, maxY - minY, 1) / 2 * 1.01;
  const root = makeQuadNode(cx, cy, half);
  ids.forEach(id => quadInsert(root, id, positions.get(id), 0));
  return root;
}
function quadRepulsion(node, id, p, idealLength2, out) {
  if (node.mass === 0) return;
  if (node.children === null) {
    if (node.leafId === id) return; // self
    let dx = p.x - node.comX, dy = p.y - node.comY;
    const dist = Math.hypot(dx, dy) || 0.01;
    const force = node.mass * idealLength2 / dist;
    out.x += (dx / dist) * force; out.y += (dy / dist) * force;
    return;
  }
  const dx = p.x - node.comX, dy = p.y - node.comY;
  const dist = Math.hypot(dx, dy) || 0.01;
  if ((node.half * 2) / dist < QUAD_THETA) {
    const force = node.mass * idealLength2 / dist;
    out.x += (dx / dist) * force; out.y += (dy / dist) * force;
  } else {
    for (const c of node.children) quadRepulsion(c, id, p, idealLength2, out);
  }
}
/* forceDirectedSettle's physics as it ran on the main thread, three steps
   per animation frame, stopping when out of steps or when a frame's last
   step's average uncapped displacement fell below the threshold. */
function referenceSettle(ids, positions, edgePairs, area, mode) {
  const idealLength = Math.sqrt(area / ids.length) * 0.5;
  const ATTRACTION_MULT = 2.2;
  const GRAVITY = 0.09;
  const degree = new Map(ids.map(id => [id, 0]));
  edgePairs.forEach(([s, t]) => { degree.set(s, (degree.get(s)||0) + 1); degree.set(t, (degree.get(t)||0) + 1); });
  const ISLAND_GRAVITY_BOOST = 16;
  const gravityMult = new Map(ids.map(id => [id, 1 + ISLAND_GRAVITY_BOOST / (1 + degree.get(id))]));
  const light = mode === "light";
  let temp = Math.sqrt(area) / (light ? 30 : 10);
  const COOLING_STEPS = light ? 30 : 180;
  let iter = 0;
  const idealLength2 = idealLength * idealLength;
  const convergeThreshold = idealLength * Math.sqrt(ids.length) * 0.04;
  function step() {
    const disp = new Map(ids.map(id => [id, { x: 0, y: 0 }]));
    const tree = buildQuadTree(ids, positions);
    ids.forEach(id => {
      const p = positions.get(id), d = disp.get(id);
      quadRepulsion(tree, id, p, idealLength2, d);
    });
    edgePairs.forEach(([s, t]) => {
      const a = positions.get(s), b = positions.get(t);
      if (!a || !b) return;
      let dx = a.x - b.x, dy = a.y - b.y;
      const dist = Math.hypot(dx, dy) || 0.01;
      const force = ATTRACTION_MULT * (dist * dist) / idealLength;
      dx = (dx / dist) * force; dy = (dy / dist) * force;
      disp.get(s).x -= dx; disp.get(s).y -= dy;
      disp.get(t).x += dx; disp.get(t).y += dy;
    });
    let cx = 0, cyy = 0;
    positions.forEach(p => { cx += p.x; cyy += p.y; });
    cx /= ids.length; cyy /= ids.length;
    ids.forEach(id => {
      const p = positions.get(id), d = disp.get(id);
      const g = GRAVITY * gravityMult.get(id);
      d.x += (cx - p.x) * g;
      d.y += (cyy - p.y) * g;
    });
    let totalForceDisp = 0;
    ids.forEach(id => {
      const p = positions.get(id), d = disp.get(id);
      const dlen = Math.hypot(d.x, d.y) || 0.01;
      const capped = Math.min(dlen, temp);
      p.x += (d.x / dlen) * capped;
      p.y += (d.y / dlen) * capped;
      totalForceDisp += dlen;
    });
    temp *= 0.96;
    iter++;
    return totalForceDisp / ids.length;
  }
  return {
    get iter() { return iter; },
    frame() {
      let avgDisp = convergeThreshold + 1;
      for (let i = 0; i < 3 && iter < COOLING_STEPS; i++) avgDisp = step();
      return !(iter < COOLING_STEPS && avgDisp > convergeThreshold);
    },
  };
}

// --- the worker, loaded twice: as plain functions, and as a worker -------
const plain = vm.createContext({ Math, Float64Array, Int32Array });
vm.runInContext(workerSrc, plain);

let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};

/* A graph shaped like a repository: a containment tree, cross-links, a few
   islands, two nodes stacked on one spot (the quadtree's depth cap). */
function makeGraph(n, seed) {
  let s = seed;
  const rnd = () => ((s = (s * 1103515245 + 12345) % 2147483648) / 2147483648);
  const ids = Array.from({ length: n }, (_, i) => "n" + i);
  const xy = new Float64Array(2 * n);
  for (let i = 0; i < n; i++) { xy[2 * i] = (rnd() - 0.5) * 900; xy[2 * i + 1] = (rnd() - 0.5) * 900; }
  xy[2] = xy[0]; xy[3] = xy[1];
  const pairs = [];
  for (let i = 1; i < n - 5; i++) pairs.push([Math.floor(rnd() * i), i]);
  for (let k = 0; k < n; k++) { const a = Math.floor(rnd() * (n - 5)), b = Math.floor(rnd() * (n - 5)); if (a !== b) pairs.push([a, b]); }
  return { ids, xy, pairs };
}
function compare(label, n, seed, mode) {
  const { ids, xy, pairs } = makeGraph(n, seed);
  const area = 1200 * 800;
  const positions = new Map(ids.map((id, i) => [id, { x: xy[2 * i], y: xy[2 * i + 1] }]));
  const ref = referenceSettle(ids, positions, pairs.map(([a, b]) => [ids[a], ids[b]]), area, mode);
  const edges = Int32Array.from(pairs.flat());
  const run = plain.createSettle(Float64Array.from(xy), edges, area, mode);
  let refDone = false, runDone = false, frames = 0, worst = 0;
  while (!(refDone && runDone) && frames < 200) {
    refDone = ref.frame(); runDone = run.frame(); frames++;
    const out = run.positions();
    ids.forEach((id, i) => {
      const p = positions.get(id);
      worst = Math.max(worst, Math.abs(p.x - out[2 * i]), Math.abs(p.y - out[2 * i + 1]));
    });
    if (refDone !== runDone) break;
  }
  check(`${label}: stops on the same step as the main-thread settle (${ref.iter} steps)`,
    refDone && runDone && ref.iter === run.iter, JSON.stringify({ ref: ref.iter, worker: run.iter, refDone, runDone }));
  check(`${label}: every position matches it at every frame (worst ${worst.toExponential(1)} units)`,
    worst < 1e-6, String(worst));
}
compare("full settle of 300 nodes", 300, 7, "full");
compare("light settle of 300 nodes", 300, 11, "light");
compare("full settle of 1,200 nodes", 1200, 3, "full");

// --- the message loop -----------------------------------------------------
class WorkerGlobalScope {}
const timers = [];
const posted = [];
const scope = new WorkerGlobalScope();
scope.postMessage = msg => posted.push(msg);
const ctx = vm.createContext({ Math, Float64Array, Int32Array, WorkerGlobalScope, self: scope,
  setTimeout: fn => { timers.push(fn); return timers.length; } });
vm.runInContext(workerSrc, ctx);
const tick = (k = 1) => { for (let i = 0; i < k && timers.length; i++) timers.shift()(); };
const send = msg => scope.onmessage({ data: msg });
const g = makeGraph(200, 5);
const settleMsg = run => ({ type: "settle", run, xy: Float64Array.from(g.xy), edges: Int32Array.from(g.pairs.flat()), area: 1200 * 800, mode: "full" });

send(settleMsg(1));
tick();
check("a settle posts the positions after its first frame",
  posted.length === 1 && posted[0].run === 1 && posted[0].xy.length === 400 && posted[0].done === false, JSON.stringify(posted.map(p => p.run)));
send({ type: "pause" });
tick(5);
check("a paused settle does no work", posted.length === 1 && timers.length === 0, posted.length);
send({ type: "resume" });
tick(3);
check("resuming carries on", posted.length === 4, posted.length);
send(settleMsg(2));
tick(1000);
check("a new settle supersedes the old one, which posts nothing more",
  posted.slice(4).every(p => p.run === 2) && posted.length > 4, JSON.stringify(posted.slice(4, 8).map(p => p.run)));
const last = posted[posted.length - 1];
check("a settle ends with one message marked done", last.done === true && posted.filter(p => p.done).length === 1, JSON.stringify(last.done));
check("...and then stops", timers.length === 0, timers.length);
posted.length = 0;
send(settleMsg(3));
tick();
send({ type: "stop" });
tick(10);
check("stop ends a settle at once", posted.length === 1 && timers.length === 0, posted.length);

console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
process.exit(failures ? 1 : 0);
