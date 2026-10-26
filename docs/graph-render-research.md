# Graph canvas render performance: research and recommendation

Issue: HaydenSchmidtDOC/DevGraph#54 ("Graph UI Render speed on larger repos").
Scope of this document: deliverable A (the libraries researched and
benchmarked) plus a recommendation and a rough implementation plan. The
rework that followed it is summarised, with before and after numbers, in
"Outcome" at the end.

## Summary

- **The current canvas is unusable past a few thousand elements, and a
  modest real repository already gets there.** With django indexed, the
  dashboard's *default* view (node ceiling 1500) puts **8,204 nodes and
  27,975 relationships** on the canvas, because the far ends (`m`) of every
  relationship of the 1500 capped nodes are added too. On that view the
  current page takes 24 s to draw its first frame, never finishes settling
  within 4 minutes, and pans at 0.1 frames per second.
- The time goes to four places, in this order: Cytoscape's own per-element
  style and draw work (gradient Bézier edges with arrows, function-valued
  styles re-evaluated on every class or data change), the layout running on
  the main thread and writing every node position into Cytoscape every frame,
  per-frame bookkeeping in the page (viewport culling toggles a class on every
  element; the glow overlay draws a radial gradient per node), and data
  handling (`mergeGraphElements` does an `O(nodes × edges)` neighbour lookup:
  5 s at 8k nodes, 108 s at 50k).
- Upgrading Cytoscape to 3.34 and turning on its WebGL renderer helps zoom
  (about 6× at 2k nodes) but not enough: it still walks every element in
  JavaScript every frame, and on the integrated GPU tested it is slower than
  the canvas renderer for panning small graphs. A clean Cytoscape page with
  the layout already moved to a worker is still at 1–3 FPS at 8–11k nodes.
- **Recommendation: progress with sigma.js (v4) on graphology, with the
  existing force-directed settle moved into a Web Worker.** With the
  browser's normal frame-rate limit, sigma panned, zoomed and dragged at
  51–60 FPS (the display's limit) on the 8–11k-node views where the current
  page manages 0.1, and it is the candidate that also brings what the
  dashboard relies on: hover labels, picking, node drag, a camera with
  animated pan/zoom/rotation, per-node and per-edge colours, curved edges and
  arrows. cosmos.gl matched it at that size and was about twice as fast at
  50k, so it is the fallback if the canvas ever has to show 50k nodes at
  once, but it has no labels and its own GPU layout would change how the
  graph looks. Several data-path fixes are needed whichever renderer is
  chosen and can ship first.

## How the current canvas works

`devgraph/dashboard/static/index.html` with Cytoscape.js 3.30.2 vendored in
`static/vendor/`. No build step; everything is in the one page.

**Look.** Dark ground (`#08090a`) with a dot grid that follows pan and zoom.
Nodes are small circles (9 px; 15 px for structural kinds such as
repositories, services, endpoints and datastores; repositories are rounded
hexagons, services rounded diamonds) coloured by category from `CAT_COLORS`.
Project-declared labels from the repository's `/schema` get their own
`user:<label>` category and colour (`applySchemaTypes`), and "Colour by
community" swaps in `COMMUNITY_PALETTE`. Edges are 1 px unbundled Bézier
curves with a linear gradient from the source's colour to the target's, at
0.35 opacity, with small triangle arrows. A separate full-screen canvas
(`#glowCanvas`) draws an additive radial-gradient halo behind every on-screen
node. Labels are hidden until something asks for them (hovering a node, or a
search highlight).

**Interaction.** Pan by dragging the background; zoom only through the +/−
buttons (wheel zoom is deliberately off); node drag is off
(`autoungrabify`). Hover brightens a node and shows its label; hovering an
entity type or relationship chip highlights that whole type. Tapping opens
the details card. Isolating a type dims everything else (`.excluded`) and
query matches glow in over 420 ms (`animateGlowTo`). An optional slow
rotation turns the whole graph about its centre.

**Data.** The canvas query (`currentStateQuery`) is
`MATCH (n) … WITH n LIMIT <ceiling> OPTIONAL MATCH (n)-[r]-(m) RETURN n, r, m, id(n) AS matchedId`
posted to `/api/cypher` with `canvas: true`. The reply has one row per
(node, relationship) pair, so every relationship arrives twice, and the
server stops at 50,000 rows. `mapGraphResultToElements` turns rows into
Cytoscape elements keyed by the server's stable identity key, and
`mergeGraphElements` diffs them into the live graph: existing elements keep
their positions, new nodes are placed from the per-repository layout cache
(`/api/repos/{id}/layout`) or next to a connected neighbour, and only then is
the layout re-settled ("light" or "full").

**Layout.** `forceDirectedSettle` is a hand-written Fruchterman–Reingold
variant with Barnes–Hut repulsion (θ 0.8), stronger attraction, island
gravity, a 180-step budget and a size-normalised convergence threshold. It
runs on the main thread, three steps per animation frame, and writes every
node's position into Cytoscape after each frame.

**Fixes that must survive any rework.**
- #2 (graph jitters after the tab was in the background): the rotation and
  settle loops stop while the tab is hidden, and the rotation clock is reset
  on `visibilitychange` so hidden time never becomes catch-up movement.
- #15 (late node arrival): every graph query bumps `glowRequestSeq`, and a
  reply that is no longer the latest is dropped *before* it touches the
  graph (`runQueryAndUpdateGraph`); schema replies have their own sequence
  (`schemaRequestSeq`).

Both live in the page's application code rather than in Cytoscape, so they
carry over unchanged as long as the new renderer is driven through the same
functions.

## Where the time goes

Profiles were taken in headless Chrome with the CPU profiler on the real
page (served by the benchmark's mock API, below).

**While loading and settling (2k nodes, 20 s sample).** About 90% of CPU time
is inside Cytoscape. The largest single item (21%) was Cytoscape's warning
logger: the edge opacity mapper (`0.35 + glow × 0.55`) went negative on the first
glow frame (see "Bugs found along the way"), and Cytoscape logs one warning
per edge for that. With DevTools attached, as under the profiler, every
console call is serialised, so in a normal tab this costs less, but it is
still one console call per element. (The benchmark runs silence the console
so this does not distort them.) The rest is style recalculation
(`parsedStyle`, `applyContextStyle`, `updateStyleHints`), which is triggered
every frame by the settle loop's position writes, by
`updateViewportCulling` toggling `.offscreen` on every node and edge, and by
`animateGlowTo` writing `data("glow")` on every element. The page's own
physics (`step`, `quadRepulsion`) was under 3% of the time; its per-frame
position writes and the culling pass were another 9%.

**While panning (2k nodes, settled).** Canvas drawing dominates: per-edge
`createLinearGradient`/`addColorStop`, Bézier strokes, and an arrowhead per
edge (`rotate`, `fill`). `renderGlow` adds about 10% (a radial gradient per
on-screen node, every frame). Style lookups still take about 8%.

**Data handling.**

| Dataset | Rows parsed | `mapGraphResultToElements` | `mergeGraphElements` | Neighbour lookup alone* |
|---|---:|---:|---:|---:|
| synthetic 2k | 9,366 | 30 ms | 0.6 s | 0.08 s |
| django default view (8.2k nodes) | 30,928 | 0.2 s | 5.1 s | 8.8 s |
| synthetic 10k | 46,724 | 0.1 s | 4.0 s | 2.8 s |
| django, ceiling released (10.7k) | 50,000 (truncated) | 0.1 s | 8.9 s | 14 s |
| synthetic 50k | 233,672 | 0.5 s | 108 s | 69 s |

\* Timed separately in Node on the same data
(`bench/graph-render/findcost.mjs`, on a loaded machine, so only the
magnitude matters; it can exceed the in-page total for the same reason): for each new node without a cached position, `mergeGraphElements`
scans every incoming relationship with `Array.find` to find one that touches
it. A `Map` built once takes 9–230 ms for the same work.

The real exported payload for the django default view is 21.7 MB for 31k
rows; the released-ceiling view is 35 MB and hits the server's 50,000-row cap
at 10.7k nodes, so today the canvas cannot be asked for more than roughly
10k nodes of a django-sized repository whatever the renderer.

The physics on its own, run headless in Node with no renderer (the same
step function, constants and quadtree lifted out of the page; a one-off
experiment, not kept in the harness), takes 54 ms a step at 2k
nodes and 250–500 ms a step at 8–11k, always reaching the 180-step budget
rather than its convergence threshold: 10 s of pure CPU at 2k and 46–92 s at
8–11k. Moving it off the main thread is necessary but not sufficient; it
also wants typed arrays and a flat quadtree (d3-force's implementation did
300 ticks of 10k nodes in about 40 s on the same machine, in a worker, with
the UI at 60 FPS throughout).

## Candidates

Vendoring rule for all of them: the dashboard ships unmodified minified files
under `static/vendor/` with a notice in `THIRD_PARTY_NOTICES.md`; nothing is
loaded from a CDN and the app has no build step. Libraries that publish only
ES modules can still be vendored as one IIFE file produced once with esbuild
(an offline, one-time step whose recipe is recorded next to the file), the
same way a minified UMD file is today.

| Library | Version (date) | Licence | Size, min / gzip | Maintenance | Vendoring |
|---|---|---|---|---|---|
| Cytoscape.js (current) | 3.30.2 in tree; 3.34.4 (Oct 2026) | MIT | 436 KB / 136 KB | Very active; WebGL renderer since 3.31 (Jan 2025) | Drop-in file swap |
| d3-force + d3-quadtree, d3-dispatch, d3-timer | 3.0.0 (2021) | ISC | 8 KB / 3 KB, plus 4 KB gzip for the three helpers | Finished and stable; last code change 2023 | UMD files, no bundling |
| sigma.js + graphology + graphology-layout-forceatlas2 | sigma 4.0.3 (Oct 2026), graphology 0.26.0, FA2 0.10.1 | MIT | 455 KB / 118 KB as one bundle (sigma alone 392 KB / 103 KB) | Very active (12k stars; v4 released 2026) | sigma ships a UMD; graphology + FA2 worker need one esbuild bundle |
| cosmos.gl (`@cosmos.gl/graph`) | 3.5.1 (Oct 2026) | MIT (OpenJS Foundation). The older `@cosmograph/cosmos` package is CC-BY-NC-4.0 and Cosmograph itself is commercial: do not use those. | 744 KB / 194 KB (includes luma.gl) | Active | UMD file |
| force-graph (vasturiano) | 1.52.0 (Sep 2026) | MIT | 173 KB / 56 KB | Active, one maintainer, 151 open issues | UMD file |
| 3d-force-graph (vasturiano) | 1.80.1 | MIT | 1.39 MB / 364 KB (three.js) | Active | UMD file |
| PixiJS (+ d3-force) | 8.22.0 (Oct 2026) | MIT | 841 KB / 237 KB | Very active | UMD file |

**Cytoscape.js 3.34 with WebGL.** The least disruptive option: same API,
same stylesheet, same events. But the WebGL renderer draws edges in a solid
`line-color` only (no gradients), arrows are always triangles (upstream
#3374), there are no shadows, and it rebuilds its instance buffers from each
element's computed style in JavaScript every frame, so it stays CPU-bound.
Upstream #3436 reports the same thing we measured: on an integrated GPU,
WebGL can be slower than the canvas renderer. Note also that Cytoscape 3.x has
no `shadow-*` style properties at all, so the dashboard's "Node/edge
shadows" toggle has never drawn anything (see bugs below).

**d3-force on canvas, optimised.** Quadtree repulsion (Barnes–Hut), alpha
decay, simulation in a Web Worker, typed-array positions, one `stroke()` per
colour, culling to the viewport, quadtree hit-testing. The layout part is
excellent (it is what everything else is measured against), but drawing tens
of thousands of curved, semi-transparent lines with Canvas 2D stays CPU- and
fill-bound: 26–37 FPS panning at 2–8k nodes and 7–11 FPS zooming at 10k on
the test machine. It would also mean writing labels, picking, drag and
camera animation ourselves.

**sigma.js + graphology + ForceAtlas2 worker.** WebGL renderer built for
exactly this (thousands to tens of thousands of nodes), with hover labels,
GPU picking, node drag, camera pan/zoom/rotation with animation, node and
edge reducers or v4 "styles" for state-dependent appearance, curved edges
and arrow heads as v4 primitives. graphology is a proper graph model
(neighbours, degrees, attributes), which suits the isolate and inspector
code. ForceAtlas2 runs in a worker and keeps the UI responsive, though its
look differs from our FR variant; the existing settle can run in a worker
instead (recommended below). Risks: v4 is a new major version with a new
styles API, and on the test machine's Mesa driver it logged
`GL_INVALID_OPERATION` warnings (drawing was correct). Its GPU picking reads
pixels back synchronously: with the browser's frame-rate limit switched off
(the first results table) that waited for a queue of unfinished frames and
stalled for 18–30 s on the first click at 50k nodes (about 2 s at 8–11k).
With the limit on, as in a normal browser, there was no such stall.

**cosmos.gl.** Runs the force simulation *and* the drawing on the GPU, so
neither scales with JavaScript. Fastest drawing of everything tested and
still 19–21 FPS panning at 50k. But it draws points (a few shapes), not labelled nodes:
labels would be a DOM or canvas overlay we write ourselves, and its many-body
simulation produces a different picture from ours (dense core, long
tendrils) that we would have to tune towards today's look. It also needs
WebGL 2 with float textures, which some locked-down or software-rendered
browsers lack.

**force-graph / 3d-force-graph.** force-graph is d3-force plus Canvas 2D in
one package; it runs the simulation on the main thread and drew 1–3 FPS at
8–11k nodes. 3d-force-graph is three.js and 3D, which is not the dashboard's
direction and is the largest download here.

**PixiJS + d3-force.** A general 2D WebGL engine. It would render as fast as
sigma, but we would build every graph feature ourselves (edges, arrows,
labels, picking, camera, culling). Not benchmarked: its rendering path is
equivalent to sigma's, and the extra work buys nothing sigma does not
already provide.

## Method

All files are in `bench/graph-render/` (see "Benchmark harness" below).

- **Data.** `gen.mjs` writes synthetic repositories shaped like DevGraph's
  real API output: the same labels, stable identity keys and row format as
  `/api/cypher` with `canvas: true` (one row per node–relationship pair), a
  CONTAINS tree, power-law CALLS mostly within packages, EXTENDS, IMPORTS,
  endpoints, datastores and documents, about 2.3 relationships per node.
  Sizes 2k, 10k and 50k nodes. For real data, django (a shallow clone) was
  registered as `zz-render-django` in a scratch DevGraph home: 47,925 nodes
  and 427k relationships (8.9 per node, mostly CALLS). Its canvas payloads
  were exported through the real dashboard API (`POST /api/cypher`), once
  with the default 1500-node ceiling ("django default view": 8,204 nodes,
  27,975 edges) and once with the ceiling released ("django released":
  10,691 nodes, 44,660 edges, truncated at 50,000 rows). The repository was
  removed from Neo4j afterwards.
- **Current renderer, as shipped.** `server.mjs` serves the real
  `index.html` and answers its boot-time API calls (repositories, schema,
  counts, an empty layout cache) and the canvas query from a payload file.
  So "current" is the real page, real code path, cold cache. `webgl` is the
  same page with Cytoscape 3.34 and `webgl: true` (two string substitutions).
- **Isolated pages** (`pages/`) render the same data with each candidate,
  matching the current look as far as each library allows (colours, sizes,
  0.35-opacity target-coloured edges, arrows where supported, labels on
  hover): `sigma` (sigma 4 + graphology, ForceAtlas2 in a worker),
  `cosmos` (cosmos.gl, GPU layout), `d3canvas` (d3-force in a worker, Canvas
  2D batched by colour, viewport culling, quadtree picking), `forcegraph`,
  and `cyweb`/`cycanvas` (Cytoscape 3.34 with the dashboard's own stylesheet
  minus the non-existent shadows, d3-force in a worker; WebGL and canvas).
- **Browser.** Chrome 152 headless (`--headless=new`) with the real GPU via
  ANGLE/OpenGL: an Intel HD Graphics 5500 (Broadwell, 2015) on a 4-core
  laptop CPU, viewport 1600×900, device pixel ratio 1. That is deliberately
  a low-end machine. Other work was running on it (load average 3–9), so
  treat differences under about 30% as noise; the differences that matter
  here are 10× to 1000×.
- **Measurements**, one fresh browser per run: *first frame* = time from
  navigation to the first frame with the graph drawn; *layout settled* =
  from data arrival to the layout's own stop signal (the current page's
  `graphSettled`, d3's alpha floor, cosmos's `onSimulationEnd`) or, for
  ForceAtlas2, which has none, until mean node movement stays under 0.03% of
  the layout's extent per 500 ms; capped at 240 s. *FPS while settling* is
  the page's `requestAnimationFrame` rate over that period (a proxy for "can
  the user do anything meanwhile"). *Pan* is a 4-second mouse drag on empty
  background; *zoom* is 4 s of continuous zoom in and out through the
  library's API (the dashboard zooms through buttons that animate the same
  way); *drag* is a 4-second mouse drag of a node (the current page has node
  drag off). *JS heap* is after a forced GC (workers and GPU memory not
  included). The main table was run with vsync and the frame-rate limit
  off so rates above 60 are visible; the second table repeats the leading
  candidates with both on, as in a normal browser, because with them off a
  WebGL page can submit frames faster than the GPU finishes them, which
  overstates its rate (and is what produced sigma's 18–30 s first-click stall
  at 50k). The second table is the one to read for WebGL renderers; it ran
  when the machine was quieter (load average under 2).

## Results

FPS figures are frames per second (higher is better); times are seconds
(lower is better). `*` = programmatic pan, used where the graph left no empty
background to drag. "> 240" = not reached within the 240 s limit. For
"sigma 4 + FA2 worker", *layout settled* is ForceAtlas2 against the stop rule
above, which fired early on some runs in the first table and hit its 120 s
cap at 10k in the second: it says how long FA2 takes, not how long the
recommended worker port of our own layout would.

Vsync and frame-rate limit off:

| Dataset | Renderer | First frame (s) | Layout settled (s) | FPS while settling | Pan FPS | Zoom FPS | Node drag FPS | JS heap (MB) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Synthetic 2k (2,000 nodes / 4,683 edges) | Current page (Cytoscape 3.30 canvas) | 6.1 | 79 | 0.8 | 11 | 1.6 | — | 35 |
| Synthetic 2k (2,000 nodes / 4,683 edges) | Current page, Cytoscape 3.34 WebGL | 4.8 | 35 | 1.6 | 7.8 | 10 | — | 33 |
| Synthetic 2k (2,000 nodes / 4,683 edges) | Cytoscape 3.34 WebGL + worker layout | 4.0 | 15 | 2.6 | 11 | 11 | 2.3 | 33 |
| Synthetic 2k (2,000 nodes / 4,683 edges) | Cytoscape 3.34 canvas + worker layout | 4.2 | 18 | 1.6 | 60 | 29 | 1.0 | 34 |
| Synthetic 2k (2,000 nodes / 4,683 edges) | sigma 4 + FA2 worker | 2.7 | 88 | 56 | 86 | 152 | 58 | 10 |
| Synthetic 2k (2,000 nodes / 4,683 edges) | cosmos.gl | 1.8 | 41 | 124 | 195 | 154 | 58 | 5 |
| Synthetic 2k (2,000 nodes / 4,683 edges) | d3-force worker + Canvas 2D | 0.5 | 21 | 51 | 37 | 20 | 19 | 2 |
| Synthetic 2k (2,000 nodes / 4,683 edges) | force-graph | 0.5 | 120 | 7.7 | 13 | 11 | 11 | 3 |
| django default view (8,204 / 27,975) | Current page (Cytoscape 3.30 canvas) | 24 | > 240 | 0.1 | 0.1 | 0.2 | — | 175 |
| django default view (8,204 / 27,975) | Current page, Cytoscape 3.34 WebGL | 24 | 226 | 0.3 | 1.8 | 1.9 | — | 156 |
| django default view (8,204 / 27,975) | Cytoscape 3.34 WebGL + worker layout | 18 | 99 | 0.8 | 1.3 | 1.8 | 0.3 | 160 |
| django default view (8,204 / 27,975) | Cytoscape 3.34 canvas + worker layout | 22 | 92 | 0.4 | 0.3 | 0.6 | 0.1 | 169 |
| django default view (8,204 / 27,975) | sigma 4 + FA2 worker | 4.5 | 72 | 25 | 49 | 83 | 78 | 40 |
| django default view (8,204 / 27,975) | cosmos.gl | 1.7 | 144 | 35 | 119 | 105 | 58 | 9 |
| django default view (8,204 / 27,975) | d3-force worker + Canvas 2D | 0.4 | 34 | 62 | 26 | 11 | 14 | 5 |
| django default view (8,204 / 27,975) | force-graph | 0.6 | 120 | 1.4 | 2.9 | 2.2 | 1.8 | 12 |
| Synthetic 10k (10,000 / 23,362) | Current page (Cytoscape 3.30 canvas) | 21 | > 240 | 0.1 | 0.1 | 0.1 | — | 156 |
| Synthetic 10k (10,000 / 23,362) | Current page, Cytoscape 3.34 WebGL | 19 | 223 | 0.3 | 2.2 | 3.2 | — | 142 |
| Synthetic 10k (10,000 / 23,362) | Cytoscape 3.34 WebGL + worker layout | 23 | 103 | 0.7 | 2.4* | 2.7 | 0.6 | 144 |
| Synthetic 10k (10,000 / 23,362) | sigma 4 + FA2 worker | 10 | 13 | 16 | 41 | 100 | 8.4 | 40 |
| Synthetic 10k (10,000 / 23,362) | cosmos.gl | 2.4 | 133 | 38 | 155 | 142 | 57 | 8 |
| Synthetic 10k (10,000 / 23,362) | d3-force worker + Canvas 2D | 0.4 | 41 | 61 | 32 | 7.7 | 1.7 | 4 |
| Synthetic 10k (10,000 / 23,362) | force-graph | 0.6 | 121 | 1.0 | 1.7 | 1.5 | 1.3 | 11 |
| django, ceiling released (10,691 / 44,660) | Current page (Cytoscape 3.30 canvas) | 33 | > 240 | 0.1 | 0.1 | 0.1 | — | 265 |
| django, ceiling released (10,691 / 44,660) | Current page, Cytoscape 3.34 WebGL | 36 | > 240 | 0.2 | 1.0 | 2.1 | — | 237 |
| django, ceiling released (10,691 / 44,660) | Cytoscape 3.34 WebGL + worker layout | 19 | 79 | 0.7 | 1.0 | 1.7 | 0.3 | 245 |
| django, ceiling released (10,691 / 44,660) | sigma 4 + FA2 worker | 3.7 | 5.7 | 26 | 31 | 89 | 57 | 60 |
| django, ceiling released (10,691 / 44,660) | cosmos.gl | 2.2 | 179 | 28 | 111 | 92 | 59 | 11 |
| django, ceiling released (10,691 / 44,660) | d3-force worker + Canvas 2D | 0.5 | 42 | 60 | 15 | 10 | 12 | 6 |
| django, ceiling released (10,691 / 44,660) | force-graph | 0.8 | 121 | 0.8 | 1.2 | 1.1 | 1.1 | 16 |
| Synthetic 50k (50,000 / 116,836) | Current page (Cytoscape 3.30 canvas) | 180 | > 240 | 0.0 | 0.0 | — (tab died) | — | 761 |
| Synthetic 50k (50,000 / 116,836) | Current page, Cytoscape 3.34 WebGL | 180 | > 240 | 0.0 | 0.0 | 0.1 | — | 685 |
| Synthetic 50k (50,000 / 116,836) | Cytoscape 3.34 WebGL + worker layout | 63 | > 240 | 0.1 | 0.1* | 0.2 | 0.1 | 706 |
| Synthetic 50k (50,000 / 116,836) | sigma 4 + FA2 worker | 8.9 | 15 | 22 | 0.3 | 121 | 15 | 173 |
| Synthetic 50k (50,000 / 116,836) | cosmos.gl | 4.6 | > 240 | 12 | 21 | 9.9 | 11 | 25 |
| Synthetic 50k (50,000 / 116,836) | d3-force worker + Canvas 2D | 1.0 | > 240 | 56 | 3.2* | 3.1 | 3.1 | 21 |

With vsync on (the browser's normal setting; rates cap at 60):

| Dataset | Renderer | First frame (s) | Layout settled (s) | FPS while settling | Pan FPS | Zoom FPS | Node drag FPS | JS heap (MB) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Synthetic 2k (2,000 nodes / 4,683 edges) | Current page (Cytoscape 3.30 canvas) | 4.6 | 67 | 0.9 | 11 | 2.4 | — | 34 |
| django default view (8,204 / 27,975) | Current page (Cytoscape 3.30 canvas) | 19 | > 240 | 0.1 | 0.1 | 0.2 | — | 175 |
| django default view (8,204 / 27,975) | sigma 4 + FA2 worker | 6.0 | 115 | 25 | 60 | 60 | 59 | 40 |
| django default view (8,204 / 27,975) | cosmos.gl | 2.8 | 168 | 30 | 52 | 60 | 60 | 9 |
| django default view (8,204 / 27,975) | d3-force worker + Canvas 2D | 0.5 | 31 | 55 | 25 | 17 | 21 | 5 |
| Synthetic 10k (10,000 / 23,362) | sigma 4 + FA2 worker | 3.2 | 122 | 37 | 60 | 56 | 59 | 42 |
| Synthetic 10k (10,000 / 23,362) | cosmos.gl | 1.4 | 155 | 32 | 53 | 54 | 55 | 8 |
| Synthetic 10k (10,000 / 23,362) | d3-force worker + Canvas 2D | 0.5 | 44 | 49 | 22* | 17 | 5.3 | 4 |
| django, ceiling released (10,691 / 44,660) | sigma 4 + FA2 worker | 3.5 | 108 | 30 | 51 | 57 | 53 | 60 |
| django, ceiling released (10,691 / 44,660) | cosmos.gl | 2.3 | 201 | 25 | 54 | 56 | 46 | 11 |
| django, ceiling released (10,691 / 44,660) | d3-force worker + Canvas 2D | 0.4 | 40 | 56 | 21 | 8.2 | 17 | 6 |
| Synthetic 50k (50,000 / 116,836) | sigma 4 + FA2 worker | 7.7 | 8.5 | 19 | 9.1 | 8.9 | 12 | 173 |
| Synthetic 50k (50,000 / 116,836) | cosmos.gl | 2.4 | > 240 | 11 | 19 | 11 | 11 | 25 |
| Synthetic 50k (50,000 / 116,836) | d3-force worker + Canvas 2D | 1.0 | > 240 | 49 | 6.2* | 1.1 | 5.7 | 21 |

Screenshots of every run are written to `bench/graph-render/shots/`.

What the numbers say:

- **Current page:** unusable from about 8k nodes, which is django's
  *default* view. Even at 2k nodes the layout takes 79 s and the page draws
  under one frame per second while it settles. At 50k the first frame came
  after 3 minutes and the tab then died.
- **Cytoscape upgraded to WebGL (`webgl`, `cyweb`):** better zoom, otherwise
  the same order of magnitude. Moving the layout off the main thread
  (`cyweb`) shortens settling, but the renderer itself stays at 1–3 FPS
  from 8k nodes. Cytoscape is the bottleneck, not just our use of it.
- **sigma:** first frame in 3–10 s (building the graphology graph plus
  shader setup), then pan, zoom and node drag at 51–60 FPS (the display's
  limit) at 8–11k real-shaped nodes, with ForceAtlas2 running in a worker
  and the page at 25–37 FPS meanwhile. At 50k it drops to 9–12 FPS on this
  GPU.
- **cosmos.gl:** the same 46–60 FPS at 8–11k and the fastest at 50k (19 FPS
  panning), but its GPU simulation takes 2.5–3.5 minutes to come to rest at
  8–11k and the result looks different from today's graph (screenshots).
- **d3-force + Canvas 2D:** the layout is good and quick in a worker
  (31–44 s at 8–11k, the UI at about 50–60 FPS meanwhile), but Canvas 2D
  drawing limits panning to about 20–25 FPS and zooming to 8–17 FPS at 8–11k
  nodes, and to single digits at 50k.

## Recommendation

**Progress, with sigma.js 4 + graphology as the renderer, and the existing
force-directed settle ported to a Web Worker.** Keep everything that is
application logic — the query building, the stable-id diff merge, the layout
cache, request supersession (#15), hidden-tab handling (#2), the schema- and
community-driven colours, isolate/dim, glow-in on query matches — and swap
only what draws and lays out the graph.

Why sigma rather than the others:

- **Performance where it matters.** Real repositories already reach 8–11k
  canvas nodes (the server's row cap stops the canvas at about that size of
  a django-like repository). There, sigma pans, zooms and drags at the
  display's 60 FPS (51–60 measured) against the current page's 0.1–0.2, and
  the UI stays responsive while the layout settles in a worker.
- **Keeps about 90% of the look with features it already has.** Same
  colours from `CAT_COLORS`/schema/community palette (sigma colours are plain
  per-node and per-edge attributes, so schema colours keep working
  unchanged), same sizes, circles, curved 0.35-opacity edges with arrows,
  labels only on hover or highlight, hover brightening, dimmed excluded
  elements. Of the two non-circle shapes, v4 ships a diamond (services);
  the repository hexagon needs a small custom shape or becomes a larger
  circle. What would change: edges in a
  single colour (the target's, as heavy mode already does today) rather than
  a source-to-target gradient — v4's `layerGradient` primitive may allow the
  gradient too, to be checked — and the halo glow is reimplemented (as a
  sigma node layer or kept on the overlay canvas, positioned through
  `graphToViewport`).
- **Fits the app's direction.** It is a graph library, not a drawing engine
  we would have to grow one in (PixiJS, raw canvas). graphology gives the
  isolate and inspector code a real graph API. MIT, actively maintained,
  vendored as two files.
- **cosmos.gl is the plan B**, not the plan: at today's sizes it performs
  the same as sigma, and it is about twice as fast at 50k nodes (19 vs 9 FPS
  panning on this GPU). If the canvas ever needs to show 50k+ nodes at once
  (that would also need the API changes below), it is the one to revisit.
  Today it would cost us labels and today's layout look.
- **Not progressing is not an option**: the current page fails on the
  default view of an ordinary large repository.
- **An optimised Cytoscape is not enough**: the data-path fixes below help
  every option, but even a clean Cytoscape 3.34 page with WebGL on and the
  layout in a worker stays at 1–3 FPS from 8k nodes.

Accepted risks, each to verify early in the implementation: sigma v4's
newness (pin 4.0.3; fall back to sigma 3.x, which has the same core model,
if v4 shows problems); the Mesa `GL_INVALID_OPERATION` warnings; GPU picking
reading pixels back synchronously (it only stalled with the frame-rate limit
off, but if it shows up on real machines, nodes can be picked on the CPU
with a quadtree as `d3canvas` does); and 9–12 FPS at 50k on an integrated
GPU, which is acceptable only because the node ceiling and the server's row
cap keep the canvas well below that.

## Implementation plan (rough)

**Step 0 — data-path fixes, renderer-independent (one small PR first).**
1. `mergeGraphElements`: build a node → first incident edge `Map` once
   instead of `allIncomingEdges.find` per node (removes 5–108 s).
2. Count the far-end `m` nodes against the node ceiling, or restrict `m` to
   the capped set, so the 1500 ceiling actually bounds the canvas
   (django's default view: 1500 asked for, 8,204 drawn).
3. Clamp `animateGlowTo`'s progress to `[0, 1]` (the first `requestAnimationFrame`
   timestamp can precede `start`, which makes every edge's opacity negative
   for one frame and logs one warning per edge).
4. Remove the `shadow-*` style properties (Cytoscape 3.x has none, so they
   are ignored with a warning) and decide whether the "Node/edge shadows"
   toggle goes or becomes the halo toggle.
5. Optionally, a compact canvas payload (nodes and relationships once each,
   instead of one row per node–relationship pair) to stay under the 50,000
   row cap for larger views. This is an API change and needs its own tests.

**Step 1 — vendor.** Add `static/vendor/sigma-4.0.3.min.js` (sigma's own UMD
build) and one esbuild IIFE of graphology 0.26.0 (plus
graphology-layout-forceatlas2 only if FA2 is kept as an option); record the
exact recipe and npm integrity hashes in `THIRD_PARTY_NOTICES.md`, as for
Cytoscape. Remove Cytoscape when nothing uses it.

**Step 2 — renderer adapter.** Introduce a small set of functions in
`index.html` that the rest of the page calls instead of `cy.*`:
add/remove/update nodes and edges, get/set positions, set state flags
(excluded, culled, highlighted, hover, label shown, glow level), camera
(pan, zoom to a point, fit to a circle with an animation, rotation angle),
and events (hover in/out, tap on node/edge/background, drag start/end).
Map the current classes to graphology attributes read by sigma styles:
`.excluded` → hollow, 0.45 opacity; `.culled`/`.offscreen` → hidden (sigma
culls the viewport itself, so `updateViewportCulling` and heavy mode largely
go away); `.hover-hl` → brighter colour and label; `.highlighted` → ring.
Keep the order of operations that #15 relies on: decide supersession before
the adapter is touched.

**Step 3 — layout worker.** Move `forceDirectedSettle` (same constants,
light/full modes, island gravity and convergence rule, so the picture keeps
its shape) into a worker using `Float32Array` positions, `Int32Array` edge
pairs and a flat array quadtree; post positions at most once per frame and
apply them in one batch. The cached layout, "settle on addition/subtraction",
and the `graphSettling` guard against rotation keep working on top. Stop the
worker while the tab is hidden (#2).

**Step 4 — effects.** Halo glow and the 420 ms glow-in, the dot grid synced
to the camera, the slow rotation as a camera angle (no more per-frame
position rewrites), hover/type highlighting, and the details card on tap.

**Step 5 — tests and docs.** The headless JS tests in `tests/dashboard/`
lift functions out of `index.html` and stub `cy` (e.g.
`query_supersession.js`, `layout_save_path.js`, `canvas_cap.js`); move them to
the adapter's interface. Re-run `bench/graph-render` against the new page as
the acceptance check (targets: 8–11k-node views pan at 30+ FPS and settle
without blocking the UI on the same low-end machine). Update `README.md`,
`PROJECT_STATUS.md` and `THIRD_PARTY_NOTICES.md`.

## Bugs found along the way

These are independent of the renderer decision (all fixed since; see
"Outcome"):

- The node ceiling does not bound the canvas: the relationships' far ends are
  added as well (1500 → 8,204 nodes on django's default view).
- `mergeGraphElements`'s neighbour lookup is `O(nodes × edges)`.
- `animateGlowTo` can compute a negative progress on its first frame, making
  every edge's opacity invalid for a frame (one Cytoscape warning per edge).
- The "Node/edge shadows" setting has no effect: Cytoscape 3.x has no
  `shadow-*` style properties. `font-family: var(--font-mono)` and the
  `var(--meta)` colours on excluded edges are likewise rejected by
  Cytoscape (it does not resolve CSS variables).

## Benchmark harness

`bench/graph-render/` (not shipped, not part of the Python package; its npm
dependencies are pinned in its own `package.json` and installed only there):

```bash
cd bench/graph-render
npm install
npm run build-sigma          # vendor/sigma-bundle.min.js for pages/sigma.html
npm run data                 # synthetic 2k / 10k / 50k payloads into data/
npm run serve &              # mock dashboard API + the real index.html on :8899
./matrix.sh                  # results/matrix.jsonl, screenshots in shots/
node table.mjs results/matrix.jsonl
```

`node bench.mjs <renderer> <dataset>` runs one case (`VSYNC=1` keeps the
browser's frame-rate limit; `CHROME=` overrides `/usr/bin/google-chrome`).
`profile.mjs` and `profpage.mjs` record CPU profiles of the real page and the
isolated pages. `fromapi.mjs` turns a payload exported from a real dashboard
(`POST /api/cypher` with `canvas: true`) into a dataset, which is how the
django sets were made; those exports are not committed.

## Outcome

The recommendation was implemented: sigma.js 4.0.3 on graphology 0.26.0
(their own UMD builds, vendored), with the page's force-directed settle in a
Web Worker (`devgraph/dashboard/static/graph-layout-worker.js`, the same
physics step for step, checked by `tests/dashboard/layout_worker.js`).
Cytoscape is gone. Everything else in the page reaches the canvas through a
small adapter, `graphView`, so the application logic (query building, the
stable-id diff merge and layout cache, schema and community colours,
isolate and dim, the glow-in, the #15 supersession check before the canvas
is touched, the #2 hidden-tab handling) is unchanged.

Renderer-independent fixes from step 0, each with a regression test:

- `mergeGraphElements` finds a new node's neighbour through a map built once
  (it read each relationship's endpoints 9 million times for 3,000 new nodes,
  now 33,000).
- The node ceiling bounds every node drawn: a capped query keeps a
  relationship only when its far end is one of the capped nodes
  (`WITH collect(n) AS capped UNWIND capped AS n ... WHERE m IN capped`). On a
  synthetic 8,000-node repository the capped query also got faster (1.2 s to
  0.2 s). The benchmark's django payloads were exported before this fix, so
  the "django default view" below still measures 8,204 nodes, a size the
  default view no longer reaches.
- The glow-in's progress is clamped to [0, 1].
- The `shadow-*` styles and the "Node/edge shadows" switch are gone; so is
  the node-glow overlay canvas and its "Node glow" switch: it was drawn every
  frame behind `#cy`, whose background is opaque, so its halos were never
  visible. The same holds for the dot grid on the graph stage, so it is no
  longer moved with the camera every frame (it stays as invisible as it
  always was). The compact payload (item 5) was not done.

Things found while porting:

- sigma's curved edges are tessellated with as many segments as the most
  detailed path in the program needs (16 curved, 32 for self-loops by
  default), for every edge: django's default view panned at about 40 frames
  per second with the defaults and at 60 with 8 segments each.
- sigma reindexes the whole canvas on every element added or removed:
  growing a 2,000-node canvas into a 10,000-node one froze the page for
  121 s (the Cytoscape page took about 13 s). The diff merge now changes the
  graph with sigma detached (`graphView.batch`) and redraws once: 1.9 s to
  grow, 1.4 s to shrink back, no task over about a second.
- Nodes can't be dragged, as under Cytoscape (`autoungrabify`): pressing on
  a node pans the view and saves no layout. (sigma's own node drag also
  re-processed every node on each move.)
- Without WebGL 2 sigma can't start; the page then uses a stand-in canvas
  that holds nothing, so the repository list, console and panels still
  work, and the canvas area says the graph view needs WebGL 2.
- The ambient rotation turns sigma's camera about the rotation pivot (the
  camera is shifted to keep the pivot in place, also after a pan) instead of
  rewriting node positions; labels stay upright, and the layout cache never
  stores rotated positions.
- The Hierarchical, Radial and Grid layouts were Cytoscape's; the page now
  computes them itself (`presetLayout`).
- Look: colours, sizes, the repository's rounded hexagon (a custom sigma node
  shape) and the service diamond, hollow dimmed excluded nodes, curved
  source-to-target gradient edges with arrows, labels only on hover or
  search, the dark hairline round every node, all kept. Differences:
  excluded edges are solid grey rather than dashed, and labels are a fixed
  12 px rather than scaling with zoom. The picture's orientation is
  Cytoscape's: sigma's y axis points up, and the adapter flips y, so stored
  coordinates and the screen agree as before.

The benchmark harness now drives the real page through `graphView` (it still
drives the pre-rework page through `cy`). Its "node drag" case presses on a
node and moves along a circle that starts at the grab point; on the real page
that pans the view, since nodes don't drag. `bench/graph-render/e2e.mjs` adds
real-browser checks that read the screen: hovering a node brightens and names
it, the page works with WebGL off, growing 2k nodes into 10k doesn't freeze
it, pressing on a node pans, the rotation keeps its pivot, nodes carry their
hairline. Its screen reads use full-page screenshots: a clipped screenshot in
headless Chrome can return an older frame of the WebGL canvas, which is what
once made a hover look like it drew nothing.

Before and after, the real page with the browser's normal frame-rate limit,
same machine (Intel HD Graphics 5500, load average under 2), one fresh
browser per run. "First frame" is from navigation; "settled" is from data
arrival to the layout's stop; FPS figures are frames per second.

| Dataset | Page | First frame (s) | Layout settled (s) | FPS while settling | Pan FPS (drag) | Zoom FPS | Node drag FPS* | JS heap (MB) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Synthetic 2k (2,000 / 4,683) | before (Cytoscape) | 4.8 | 63 | 1.0 | 9.9 | 2.0 | — (off) | 34 |
| Synthetic 2k (2,000 / 4,683) | after (sigma) | 4.9 | 4.3 | 48 | 60 | 59 | 57 | 12 |
| django default view (8,204 / 27,975) | before | 20.6 | > 240 | 0.1 | 0.1 | 0.2 | — (off) | 175 |
| django default view (8,204 / 27,975) | after | 5.9 | 21 | 48 | 54 | 58 | 51 | 49 |
| Synthetic 10k (10,000 / 23,362) | before | 21.1 | > 240 | 0.1 | 0.1 | 0.2 | — (off) | 156 |
| Synthetic 10k (10,000 / 23,362) | after | 5.5 | 24 | 49 | 47 | 32 | 49 | 46 |
| Synthetic 50k (50,000 / 116,836) | before (first table above) | 180 | > 240 | 0.0 | 0.0 | — (tab died) | — | 761 |
| Synthetic 50k (50,000 / 116,836) | after | 12.9 | 149 | 46 | 8.9 | 4.9 | 10 | 210 |
| django default view (8,204 / 27,975) | after the review fixes | 6.8 | 20 | 49 | 53 | 58 | 49 (pans) | 49 |
| Synthetic 10k (10,000 / 23,362) | after the review fixes | 5.3 | 24 | 48 | 49 | 35 | 46 (pans) | 46 |

\* The "after" rows above the last two were measured while the page still
let nodes be dragged; the last two press on a node, which now pans.

With the frame-rate limit off (the first table's setting) the new page drew
django's default view at 212 FPS panning through the API (62 with the mouse,
whose events arrive at about that rate), 137 zooming and 64 dragging; the
10k view at 230, 155 and 58; and the 50k view at 51, 34 and 59.

Targets met at the sizes the canvas shows: at 8-10k nodes, pan and zoom (and
pressing on a node) are at 35-58 FPS with the frame-rate limit on (at least 30), and the page
stays at about 48 FPS while the layout settles, where it was at 0.1. At 50k
nodes the GPU, not JavaScript, is the limit (single-digit FPS), as the
research predicted; the node ceiling and the server's row cap keep the canvas
far below that. The layout still takes 20-25 s to settle at 8-10k nodes and
2.5 minutes at 50k: moving it off the main thread made it harmless, not
faster (it is the same physics, about 0.4 s a step at 8k nodes).

