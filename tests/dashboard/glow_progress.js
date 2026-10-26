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
const mkEle = (id, isNode) => {
  const data = { glow: 0 };
  return {
    id: () => id, isNode: () => isNode, hasClass: () => false,
    data: (k, v) => { if (v === undefined) return data[k]; if (k === "glow") written.push(v); data[k] = v; },
  };
};
const eles = [mkEle("a", true), mkEle("b", true), mkEle("e", false)];
const frames = [];
let now = 1000;
const globals = {
  cy: { elements: () => eles },
  performance: { now: () => now },
  requestAnimationFrame: fn => { frames.push(fn); return frames.length; },
  cancelAnimationFrame: () => {},
  setNodeGlowActive: () => {},
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
  eles[0].data("glow") === 1 && eles[2].data("glow") === 1 && eles[1].data("glow") === 0,
  JSON.stringify(eles.map(e => e.data("glow"))));

console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
process.exit(failures ? 1 : 0);
