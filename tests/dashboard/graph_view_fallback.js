/* The stand-in canvas used without WebGL 2 (createNullGraphView, lifted out
   of index.html) must answer every call the real one (createGraphView's view)
   does, or the page dies on the first call it lacks -- the whole dashboard,
   not just the canvas. A real-browser run with WebGL off is in
   bench/graph-render/e2e.mjs (nowebgl). */
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
const realView = grab(/^  const view = \{/m, "\n  };");
const realMethods = [...realView.matchAll(/^    (?:async )?([A-Za-z]+)(?:\(|:)/gm)].map(m => m[1]).filter(k => !["renderer", "graph", "camera"].includes(k));
const nullView = new Function(grab(/^function createNullGraphView\(/m, "\n}") + "\nreturn createNullGraphView();")();

let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};
check("the real view's methods were found", realMethods.length > 30, realMethods.join(","));
const missing = realMethods.filter(k => typeof nullView[k] !== "function");
check("the stand-in has every method of the real view", !missing.length, missing.join(","));
check("...and holds nothing", nullView.nodeCount() === 0 && nullView.nodeIds().length === 0 && nullView.elementIds().length === 0, "");
let done = false;
nullView.animatePositions({}, 500, () => { done = true; });
check("...and finishes an animation it can't draw at once", done, "");

console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
process.exit(failures ? 1 : 0);
