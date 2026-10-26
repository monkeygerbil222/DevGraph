/* Headless test of the page's side of the force-directed settle
   (forceDirectedSettle and the functions around it, lifted verbatim out of
   index.html): what it sends the layout worker, and how it applies what
   comes back.

   It must apply positions on animation frames, at most once a frame, so a
   hidden tab -- where Chrome runs no animation frames -- never moves the
   graph and then jumps (#2), and it must pause the worker while the tab is
   hidden. A settle superseded by a newer one must not touch the graph, and
   only the end of a settle marks the graph settled, saves the layout and
   calls back. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const html = fs.readFileSync(
  path.join(__dirname, "..", "..", "devgraph", "dashboard", "static", "index.html"), "utf8");
const grab = (startRe, endMarker) => {
  const i = html.search(startRe);
  if (i < 0) throw new Error("could not find " + startRe);
  const j = html.indexOf(endMarker, i);
  if (j < 0) throw new Error("could not find end marker after " + startRe);
  return html.slice(i, j + endMarker.length);
};
const src = grab(/^let layoutWorker = null;/m, "\n});\n");

const workers = [];
class FakeWorker {
  constructor(url) { this.url = url; this.sent = []; workers.push(this); }
  postMessage(msg) { this.sent.push(msg); }
}
const frames = [];
const listeners = {};
const applied = [];
const calls = [];
const nodes = new Map([["a", { x: 10, y: 20 }], ["b", { x: 0, y: 0 }], ["c", { x: -30, y: 5 }]]);
const context = {
  Worker: FakeWorker, Math, Map, Float64Array, Int32Array,
  requestAnimationFrame: fn => { frames.push(fn); return frames.length; },
  cancelAnimationFrame: () => {},
  document: { hidden: false, addEventListener: (event, fn) => { listeners[event] = fn; } },
  graphView: {
    nodeIds: () => [...nodes.keys()],
    position: id => ({ ...nodes.get(id) }),
    size: () => ({ width: 1000, height: 600 }),
    setView: () => {},
    forEachEdge: fn => { fn("e1", {}, "a", "b"); fn("e2", {}, "b", "c"); },
    setPositions: (ids, xy) => applied.push({ ids: [...ids], xy: [...xy] }),
  },
  rotationPivot: null, graphSettling: false,
  setGraphSettled: v => calls.push("settled:" + v),
  saveCachedLayout: () => calls.push("save"),
  updateOnscreenCount: () => {},
};
vm.createContext(context);
vm.runInContext(src, context);

let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};
const reply = (run, xy, done) => workers[0].onmessage({ data: { type: "positions", run, xy: Float64Array.from(xy), done } });
const runFrames = () => { while (frames.length) frames.shift()(); };

let doneCalls = 0;
context.forceDirectedSettle(() => doneCalls++, "full");
const start = workers[0].sent[0];
check("a settle starts the worker served from the package", workers.length === 1 && workers[0].url === "/static/graph-layout-worker.js", workers[0] && workers[0].url);
check("...with every node's position, unplaced ones seeded around the origin",
  start.type === "settle" && start.xy.length === 6 && start.xy[0] === 10 && start.xy[1] === 20 && start.xy[4] === -30 &&
  (start.xy[2] !== 0 || start.xy[3] !== 0), JSON.stringify(start && [...start.xy]));
check("...the edges as node index pairs, the mode and the area",
  JSON.stringify([...start.edges]) === "[0,1,1,2]" && start.mode === "full" && start.area === 600000, JSON.stringify(start));
check("the graph is marked unsettled and settling", calls[0] === "settled:false" && context.graphSettling === true, JSON.stringify(calls));

reply(1, [1, 1, 2, 2, 3, 3], false);
reply(1, [4, 4, 5, 5, 6, 6], false);
check("positions wait for an animation frame", applied.length === 0 && frames.length === 1, applied.length);
runFrames();
check("one frame applies only the latest positions", applied.length === 1 && applied[0].xy.join() === "4,4,5,5,6,6",
  JSON.stringify(applied));

context.document.hidden = true;
listeners.visibilitychange();
check("hiding the tab pauses the worker", workers[0].sent.at(-1).type === "pause", JSON.stringify(workers[0].sent.at(-1)));
context.document.hidden = false;
listeners.visibilitychange();
check("showing it again resumes the worker", workers[0].sent.at(-1).type === "resume", JSON.stringify(workers[0].sent.at(-1)));

let secondDone = 0;
context.forceDirectedSettle(() => secondDone++, "light");
check("a second settle reuses the worker with a new run", workers.length === 1 && workers[0].sent.at(-1).run === 2, workers.length);
applied.length = 0;
reply(1, [9, 9, 9, 9, 9, 9], true);
runFrames();
check("a superseded settle's positions and end are ignored",
  applied.length === 0 && doneCalls === 0 && !calls.includes("settled:true"), JSON.stringify({ applied, doneCalls, calls }));

reply(2, [7, 7, 8, 8, 9, 9], true);
check("the end of a settle waits for an animation frame too", !calls.includes("settled:true"), JSON.stringify(calls));
runFrames();
check("the end applies the final positions", applied.length === 1 && applied[0].xy.join() === "7,7,8,8,9,9", JSON.stringify(applied));
check("...marks the graph settled, saves the layout and calls back once",
  calls.slice(-2).join() === "settled:true,save" && secondDone === 1 && context.graphSettling === false,
  JSON.stringify({ calls, secondDone, settling: context.graphSettling }));

context.document.hidden = true;
context.forceDirectedSettle(() => {}, "full");
check("a settle started in a hidden tab starts paused", workers[0].sent.at(-1).type === "pause", JSON.stringify(workers[0].sent.at(-1)));

// the worker fails: the settle ends as if done, without saving, and the
// next settle starts a new worker
context.document.hidden = false;
const logged = [];
context.console = { error: (...a) => logged.push(a.join(" ")) };
let failedDone = 0;
calls.length = 0;
context.forceDirectedSettle(() => failedDone++, "full");
const failing = workers[workers.length - 1];
failing.onerror({ message: "boom" });
check("a failed worker ends the settle: settling stops and its callback runs once",
  context.graphSettling === false && failedDone === 1, JSON.stringify({ settling: context.graphSettling, failedDone }));
check("...the half-settled layout is not saved", !calls.includes("save"), JSON.stringify(calls));
check("...the failure is logged", logged.some(l => /boom/.test(l)), JSON.stringify(logged));
reply(failing.sent.at(-1).run, [1, 2, 3, 4, 5, 6], true);
runFrames();
check("...a late reply from it changes nothing", failedDone === 1 && !calls.includes("save"), JSON.stringify(calls));
context.forceDirectedSettle(() => {}, "full");
check("the next settle starts a new worker", workers.length === 2 && workers[1].sent[0].type === "settle", workers.length);

console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
process.exit(failures ? 1 : 0);
