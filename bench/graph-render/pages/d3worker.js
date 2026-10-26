// d3-force simulation off the main thread: positions stream back as a
// transferable Float32Array a few times per frame budget.
importScripts("/bench/node_modules/d3-dispatch/dist/d3-dispatch.min.js", "/bench/node_modules/d3-quadtree/dist/d3-quadtree.min.js",
  "/bench/node_modules/d3-timer/dist/d3-timer.min.js", "/bench/node_modules/d3-force/dist/d3-force.min.js");
let sim, nodes, running = false;
function post() {
  const xy = new Float32Array(nodes.length * 2);
  for (let i = 0; i < nodes.length; i++) { xy[2 * i] = nodes[i].x; xy[2 * i + 1] = nodes[i].y; }
  postMessage({ xy, alpha: sim.alpha(), done: sim.alpha() < sim.alphaMin() }, [xy.buffer]);
}
function loop() {
  const t0 = performance.now();
  while (performance.now() - t0 < 14 && sim.alpha() >= sim.alphaMin()) sim.tick();
  post();
  if (sim.alpha() >= sim.alphaMin()) setTimeout(loop, 0); else running = false;
}
onmessage = e => {
  const m = e.data;
  if (m.type === "init") {
    nodes = Array.from({ length: m.n }, (_, i) => ({ x: m.xy[2 * i], y: m.xy[2 * i + 1] }));
    const links = []; for (let k = 0; k < m.links.length; k += 2) links.push({ source: m.links[k], target: m.links[k + 1] });
    sim = d3.forceSimulation(nodes)
      .force("charge", d3.forceManyBody().strength(-30).theta(0.9))
      .force("link", d3.forceLink(links).distance(25))
      .force("x", d3.forceX().strength(0.05)).force("y", d3.forceY().strength(0.05))
      .stop();
    running = true; loop();
  } else if (m.type === "drag") {
    const n = nodes[m.i]; n.fx = m.x; n.fy = m.y; sim.alphaTarget(0.1); if (sim.alpha() < 0.1) sim.alpha(0.1);
    if (!running) { running = true; loop(); }
  } else if (m.type === "dragend") {
    const n = nodes[m.i]; n.fx = n.fy = null; sim.alphaTarget(0);
  }
};
