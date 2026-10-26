/* Headless test of the glow-in animation (animateGlowTo, lifted verbatim out
   of index.html): every glow value it writes stays within [0, 1].

   The first requestAnimationFrame timestamp is the frame's start time, which
   can precede the performance.now() the animation started at. The progress
   was then negative, the eased value below zero, and every edge's opacity
   (0.35 + glow * 0.55) invalid for a frame, logging one warning per edge. */
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
const src = [
  grab(/^let glowAnimId = null;/m, ";"),
  grab(/^function animateGlowTo\(targetIds\)/m, "\n}"),
].join("\n");

const written = [];
/* the canvas's elements: attribute objects, as graphView hands them out */
const mk = (id, isNode) => ({ id, isNode, attrs: { glow: 0 } });
const eles = [mk("a", true), mk("b", true), mk("e", false)];
const record = (fn, list) => list.forEach(e => { fn(e.id, e.attrs); written.push(e.attrs.glow); });
const frames = [];
let now = 1000;
const globals = {
  graphView: {
    forEachNode: fn => eles.filter(e => e.isNode).forEach(e => fn(e.id, e.attrs)),
    forEachEdge: fn => eles.filter(e => !e.isNode).forEach(e => fn(e.id, e.attrs)),
    updateNodes: fn => record(fn, eles.filter(e => e.isNode)),
    updateEdges: fn => record(fn, eles.filter(e => !e.isNode)),
  },
  performance: { now: () => now },
  requestAnimationFrame: fn => { frames.push(fn); return frames.length; },
  cancelAnimationFrame: () => {},
};
const animateGlowTo = new Function(...Object.keys(globals), src + "\nreturn animateGlowTo;")(...Object.values(globals));

let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};

animateGlowTo(["a", "e"]);
// The first frame's timestamp is that frame's start, 8 ms before the call.
frames.shift()(now - 8);
check("a frame stamped before the animation started writes no negative glow",
  written.every(v => v >= 0 && v <= 1), JSON.stringify(written));
while (frames.length) frames.shift()(now += 100);
check("every glow value written stays within [0, 1]", written.every(v => v >= 0 && v <= 1), JSON.stringify(written));
check("matched elements end fully glowing, the rest dark",
  eles[0].attrs.glow === 1 && eles[2].attrs.glow === 1 && eles[1].attrs.glow === 0,
  JSON.stringify(eles.map(e => e.attrs.glow)));

console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
process.exit(failures ? 1 : 0);
