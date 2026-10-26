/* Force-directed settle for the dashboard canvas, run in a Web Worker so the
   page stays responsive while a large graph settles (index.html starts it
   through forceDirectedSettle). The physics is the page's own: a uniform
   Fruchterman-Reingold variant with stronger attraction, island gravity,
   Barnes-Hut repulsion and a size-normalised convergence rule -- the same
   constants and the same order of operations it had on the main thread,
   so the picture keeps its shape. Only the data layout changed: typed
   arrays and a flat quadtree instead of Maps and objects. The reasoning
   behind each constant is recorded next to it below.

   Messages in:  {type: "settle", run, xy: Float64Array(2n), edges: Int32Array(2e), area, mode}
                 {type: "stop"}, {type: "pause"}, {type: "resume"}
   Messages out: {type: "positions", run, xy: Float64Array(2n), done}
   A settle posts the positions after every frame's worth of steps (3, as
   the main-thread loop ran per animation frame) and once more, with done,
   when it converges or runs out of steps. */
"use strict";

/* uniform Fruchterman-Reingold force settle — every node repels every other
   node by the same rule and every real edge pulls its two ends together by
   the same rule, with no notion of "connected component" anywhere in the
   math. This is deliberately not cose: cose bins disconnected components
   into their own separately-packed grid (the "islands line up outside the
   cluster" bug, and tethering them in with a fake edge just hides them
   instead of actually placing them well). Here an island is just a node (or
   small group of nodes) with no springs pulling it — pure repulsion from
   every other node plus mild centering gravity settles it into open space
   around the main mass on its own, the same way it would for every other
   node. Runs in a Web Worker, so it never blocks the page. */
/* Repulsion and attraction are deliberately DIFFERENT strengths, not the
   textbook symmetric Fruchterman-Reingold (repulsion = idealLength²/dist,
   attraction = dist²/idealLength, same constant both ways) — pure FR at a
   reasonable idealLength was pushing connected nodes apart almost as hard as
   it pulled them together, which reads as "only repulsion" once several
   hundred nodes are all pushing on each other at once. ATTRACTION_MULT makes
   real edges pull noticeably harder than the symmetric baseline so connected
   clusters actually pull tight, GRAVITY is much stronger so separate
   clusters/islands (nothing but repulsion + gravity acting on them) get
   pulled in close instead of drifting out to wherever repulsion alone would
   put them, and idealLength itself is smaller so the whole graph sits
   tighter overall. mode:"light" is for a future incremental reindex (a few
   nodes added to an already-settled graph) — short, low-temperature run that
   nudges things into place without re-shuffling a graph that was already
   fine. mode:"full" (default) is for a first load or a materially different
   node set. */
/* Barnes-Hut spatial partitioning for the repulsion phase below. Naive
   repulsion is all-pairs (every node checks every other node), which is
   O(n^2) per step -- fine at a few hundred nodes, but measured at ~360ms
   per step at "All Repos" scale (~3300 nodes, ~5.3M pairs), meaning a full
   settle blocked the main thread for over a second per animation frame,
   repeated ~47 times. A quadtree groups nearby nodes into cells and lets a
   node treat a whole distant cell as one point (its center of mass) instead
   of visiting every node in it -- O(n log n) instead of O(n^2), the
   standard fix for force-directed layouts at this scale (same technique
   d3-force and sigma.js use). THETA controls the accuracy/speed tradeoff:
   a cell is only approximated (rather than recursed into) once its size
   relative to distance from the query point is below THETA -- 0.8 trades a
   small amount of physical accuracy (repulsion is meant to look organic,
   not exact) for a large speedup. */
const QUAD_THETA = 0.8;
const QUAD_MAX_DEPTH = 24;
const ATTRACTION_MULT = 2.2;
const GRAVITY = 0.09;
const ISLAND_GRAVITY_BOOST = 16;
const STEPS_PER_FRAME = 3;

/* A Barnes-Hut quadtree in flat arrays. Cell c covers the square centred on
   (cx[c], cy[c]) with half-width half[c]; child[c] is the first of its four
   children (top-left, top-right, bottom-left, bottom-right) or -1 for a
   leaf, and leaf[c] the one point a leaf holds (-1 for none, or for a cell
   at the depth cap holding several, whose centre of mass is their centroid). */
function createQuadTree() {
  let cap = 0, used = 0;
  let cx, cy, half, mass, comX, comY, child, leaf;
  function grow(need) {
    if (need <= cap) return;
    const n = Math.max(need, cap * 2, 64);
    const f = old => { const a = new Float64Array(n); if (old) a.set(old); return a; };
    const i = old => { const a = new Int32Array(n); if (old) a.set(old); return a; };
    cx = f(cx); cy = f(cy); half = f(half); mass = f(mass); comX = f(comX); comY = f(comY);
    child = i(child); leaf = i(leaf);
    cap = n;
  }
  function cell(x, y, h) {
    grow(used + 1);
    const c = used++;
    cx[c] = x; cy[c] = y; half[c] = h; mass[c] = 0; comX[c] = 0; comY[c] = 0; child[c] = -1; leaf[c] = -1;
    return c;
  }
  function childFor(c, x, y) {
    return child[c] + (y >= cy[c] ? 2 : 0) + (x >= cx[c] ? 1 : 0);
  }
  function insert(c, id, x, y, depth) {
    if (mass[c] === 0 && child[c] === -1) {
      mass[c] = 1; comX[c] = x; comY[c] = y; leaf[c] = id;
      return;
    }
    if (child[c] === -1) {
      if (depth >= QUAD_MAX_DEPTH) leaf[c] = -1;
      else {
        const oldId = leaf[c], ox = comX[c], oy = comY[c];
        leaf[c] = -1;
        const h = half[c] / 2, x0 = cx[c], y0 = cy[c];
        // allocate the four children contiguously, in quadrant order
        const first = cell(x0 - h, y0 - h, h);
        cell(x0 + h, y0 - h, h); cell(x0 - h, y0 + h, h); cell(x0 + h, y0 + h, h);
        child[c] = first;
        insert(childFor(c, ox, oy), oldId, ox, oy, depth + 1);
      }
    }
    if (child[c] !== -1) insert(childFor(c, x, y), id, x, y, depth + 1);
    comX[c] = (comX[c] * mass[c] + x) / (mass[c] + 1);
    comY[c] = (comY[c] * mass[c] + y) / (mass[c] + 1);
    mass[c] += 1;
  }
  function build(n, px, py) {
    used = 0;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (let i = 0; i < n; i++) {
      const x = px[i], y = py[i];
      if (x < minX) minX = x; if (x > maxX) maxX = x;
      if (y < minY) minY = y; if (y > maxY) maxY = y;
    }
    const root = cell((minX + maxX) / 2, (minY + maxY) / 2, Math.max(maxX - minX, maxY - minY, 1) / 2 * 1.01);
    for (let i = 0; i < n; i++) insert(root, i, px[i], py[i], 0);
    return root;
  }
  function repulsion(c, id, x, y, idealLength2, out) {
    if (mass[c] === 0) return;
    if (child[c] === -1) {
      if (leaf[c] === id) return; // self
      const dx = x - comX[c], dy = y - comY[c];
      const dist = Math.hypot(dx, dy) || 0.01;
      const force = mass[c] * idealLength2 / dist;
      out[0] += (dx / dist) * force; out[1] += (dy / dist) * force;
      return;
    }
    const dx = x - comX[c], dy = y - comY[c];
    const dist = Math.hypot(dx, dy) || 0.01;
    if ((half[c] * 2) / dist < QUAD_THETA) {
      const force = mass[c] * idealLength2 / dist;
      out[0] += (dx / dist) * force; out[1] += (dy / dist) * force;
    } else {
      const first = child[c];
      for (let k = 0; k < 4; k++) repulsion(first + k, id, x, y, idealLength2, out);
    }
  }
  return { build, repulsion };
}

/* One settle run over n nodes (positions interleaved in xy, already placed
   by the page) and the edge index pairs in `edges`. step() advances the
   physics by one iteration and returns the average uncapped displacement. */
function createSettle(xy, edges, area, mode) {
  const n = xy.length / 2, m = edges.length / 2;
  const px = new Float64Array(n), py = new Float64Array(n);
  for (let i = 0; i < n; i++) { px[i] = xy[2 * i]; py[i] = xy[2 * i + 1]; }
  const idealLength = Math.sqrt(area / n) * 0.5;
  const idealLength2 = idealLength * idealLength;
  const degree = new Float64Array(n);
  for (let k = 0; k < m; k++) { degree[edges[2 * k]]++; degree[edges[2 * k + 1]]++; }
  /* a connected node gets pulled toward the cluster TWICE — gravity, and the
     spring on every one of its edges. An island has no edges, so gravity is
     its only pull against the same repulsion everyone else feels — at equal
     gravity it settles measurably farther out than a connected node would,
     and the tighter attraction now pulling the main mass into a smaller
     cluster only makes that gap look bigger. Scale gravity up for low-degree
     nodes specifically so an island is held in about as tightly, relative to
     the cluster, as a connected node is. */
  const gravity = new Float64Array(n);
  for (let i = 0; i < n; i++) gravity[i] = GRAVITY * (1 + ISLAND_GRAVITY_BOOST / (1 + degree[i]));
  const light = mode === "light";
  let temp = Math.sqrt(area) / (light ? 30 : 10);
  /* A hard step budget, not the thing that decides when settling is done --
     see convergeThreshold below for that. This is only a safety net against
     a graph that genuinely never quiets down (or the theta-approximated
     repulsion oscillating forever), so it's set generously high rather than
     tuned to "how many steps a typical graph needs": a graph the size of
     devgraph converges in well under this on its own; a much larger one
     (RAG4's scale) gets the room to actually finish instead of getting cut
     off mid-motion, which is what a low fixed cap here used to do -- the
     cooldown schedule below still drives temp near zero by the time this
     many steps run, so it was quietly masquerading as "converged" while
     really just "out of steps." */
  const coolingSteps = light ? 30 : 180;
  /* Average per-node displacement below which the layout reads as stopped --
     scaled off idealLength (which already scales with graph size/viewport
     area, see above) rather than a bare pixel constant, so a small tight
     graph and a large sparse one both stop at "no longer visibly moving"
     instead of the large one either stopping too early or burning steps it
     doesn't need. Measured against the UNCAPPED force displacement computed
     in step() below (dlen, before it's clamped to the current `temp`), not
     the clamped movement actually applied to positions -- `temp` decays every
     step regardless of whether the layout is anywhere near equilibrium, so
     capped movement shrinks toward zero on its own late in a run even while
     the real forces (and so the true, still-wanted displacement) stay large.
     Checking the capped value read that shrinkage as "settled" and froze the
     frame mid-motion on anything that didn't fully resolve within the old,
     much lower step cap -- most visibly on a bigger graph like RAG4.
     idealLength itself is sqrt(area/n) * 0.5 -- it shrinks as node count n
     grows, since the same viewport area has to fit more nodes. Multiplying
     the tolerance by idealLength alone therefore made the threshold get
     STRICTER on a bigger graph (a smaller and smaller average displacement
     required to call it settled) at exactly the point where there's more
     going on to average over -- more repulsion pairs, more Barnes-Hut
     approximation error -- so a big graph's residual jitter had a harder
     bar to clear, not an easier one. Multiplying back in sqrt(ids.length)
     cancels that shrinkage (idealLength * sqrt(n) is flat in n, since
     idealLength itself is proportional to 1/sqrt(n)), so the tolerance is a
     roughly constant number of screen pixels of average residual pull
     regardless of graph size, instead of one that keeps getting harder to
     hit as the graph grows. The 0.01 constant (was 0.004) is the actual
     "how close to zero counts as zero" tolerance increase. */
  const convergeThreshold = idealLength * Math.sqrt(n) * 0.04;
  const dx_ = new Float64Array(n), dy_ = new Float64Array(n);
  const tree = createQuadTree();
  const out = [0, 0];
  const run = { iter: 0, coolingSteps, convergeThreshold, px, py };
  run.step = () => {
    const root = tree.build(n, px, py);
    for (let i = 0; i < n; i++) {
      out[0] = 0; out[1] = 0;
      tree.repulsion(root, i, px[i], py[i], idealLength2, out);
      dx_[i] = out[0]; dy_[i] = out[1];
    }
    for (let k = 0; k < m; k++) {
      const s = edges[2 * k], t = edges[2 * k + 1];
      let ex = px[s] - px[t], ey = py[s] - py[t];
      const dist = Math.hypot(ex, ey) || 0.01;
      const force = ATTRACTION_MULT * (dist * dist) / idealLength;
      ex = (ex / dist) * force; ey = (ey / dist) * force;
      dx_[s] -= ex; dy_[s] -= ey;
      dx_[t] += ex; dy_[t] += ey;
    }
    let cx = 0, cy = 0;
    for (let i = 0; i < n; i++) { cx += px[i]; cy += py[i]; }
    cx /= n; cy /= n;
    for (let i = 0; i < n; i++) {
      dx_[i] += (cx - px[i]) * gravity[i];
      dy_[i] += (cy - py[i]) * gravity[i];
    }
    let total = 0;
    for (let i = 0; i < n; i++) {
      const dlen = Math.hypot(dx_[i], dy_[i]) || 0.01;
      const capped = Math.min(dlen, temp);
      px[i] += (dx_[i] / dlen) * capped;
      py[i] += (dy_[i] / dlen) * capped;
      total += dlen; // uncapped -- see convergeThreshold above
    }
    temp *= 0.96;
    run.iter++;
    return total / n;
  };
  /* One animation frame's worth of steps; true once the run should stop:
     out of steps, or below the convergence threshold on a frame's last step. */
  run.frame = () => {
    let avgDisp = convergeThreshold + 1;
    for (let i = 0; i < STEPS_PER_FRAME && run.iter < coolingSteps; i++) avgDisp = run.step();
    return !(run.iter < coolingSteps && avgDisp > convergeThreshold);
  };
  run.positions = () => {
    const outXY = new Float64Array(2 * n);
    for (let i = 0; i < n; i++) { outXY[2 * i] = px[i]; outXY[2 * i + 1] = py[i]; }
    return outXY;
  };
  return run;
}

if (typeof WorkerGlobalScope !== "undefined" && self instanceof WorkerGlobalScope) {
  let current = null, paused = false, timer = null;
  const loop = () => {
    timer = null;
    if (!current || paused) return;
    const done = current.settle.frame();
    const xy = current.settle.positions();
    self.postMessage({ type: "positions", run: current.run, xy, done }, [xy.buffer]);
    if (done) current = null;
    else timer = setTimeout(loop, 0);
  };
  const kick = () => { if (!timer && current && !paused) timer = setTimeout(loop, 0); };
  self.onmessage = e => {
    const msg = e.data;
    if (msg.type === "settle") {
      current = { run: msg.run, settle: createSettle(msg.xy, msg.edges, msg.area, msg.mode) };
      kick();
    } else if (msg.type === "stop") {
      current = null;
    } else if (msg.type === "pause") {
      paused = true;
    } else if (msg.type === "resume") {
      paused = false;
      kick();
    }
  };
}
