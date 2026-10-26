/* Prints the canvas state query index.html builds (currentStateQuery, lifted
   verbatim) for the state given as JSON on argv[2]:
   {"repo", "nodeCeiling", "nodeMax", "relCeiling", "relMax", "isolatedEntity", "isolatedRel"}.
   test_canvas_ceiling_live.py runs the printed query against Neo4j.
   Run with no argument (as CI runs every harness here), it checks the query's
   shape instead: a capped view keeps only relationships between capped
   nodes, an uncapped one is left as it was. */
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
const src = [
  grab(/^function escapeCypherStr\(/m, "\n"),
  grab(/^let REGISTERED_REPO_IDS = /m, "\n"),
  grab(/^function allReposClause\(/m, "\n}"),
  grab(/^function currentStateQuery\(/m, "\n}"),
].join("\n");

const queryFor = s => {
  const els = {
    repoSelect: { value: s.repo },
    ctlNodeCeiling: { max: s.nodeMax }, ctlRelCeiling: { max: s.relMax },
  };
  const globals = {
    document: { getElementById: id => els[id] || null },
    state: { isolatedEntity: s.isolatedEntity || null, isolatedRel: s.isolatedRel || null,
      nodeCeiling: s.nodeCeiling, relCeiling: s.relCeiling },
  };
  const currentStateQuery = new Function(...Object.keys(globals), src + "\nreturn currentStateQuery;")(
    ...Object.values(globals));
  return currentStateQuery();
};

if (process.argv[2]) {
  process.stdout.write(queryFor(JSON.parse(process.argv[2])));
} else {
  let failures = 0;
  const check = (label, cond, detail) => {
    console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
    if (!cond) failures++;
  };
  const base = { repo: "r", nodeMax: 100, relCeiling: 1000, relMax: 1000 };
  const capped = queryFor({ ...base, nodeCeiling: 10 });
  check("a capped view collects its capped nodes",
    /WITH n LIMIT 10\nWITH collect\(n\) AS capped\nUNWIND capped AS n/.test(capped), capped);
  check("...and keeps a relationship only when its far end is one of them", /OPTIONAL MATCH \(n\)-\[r\]-\(m\)\nWHERE m IN capped/.test(capped), capped);
  const rel = queryFor({ ...base, nodeCeiling: 10, isolatedRel: "CALLS" });
  check("...with a relationship type isolated too", /WHERE type\(r\) = "CALLS" AND m IN capped/.test(rel), rel);
  const open = queryFor({ ...base, nodeCeiling: 100 });
  check("an uncapped view keeps every relationship", !/capped/.test(open) && /OPTIONAL MATCH \(n\)-\[r\]-\(m\)\nRETURN/.test(open), open);
  console.log(failures ? "\n" + failures + " FAILED" : "\nall passed");
  process.exit(failures ? 1 : 0);
}
