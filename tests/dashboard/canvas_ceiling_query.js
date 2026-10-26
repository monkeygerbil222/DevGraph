/* Prints the canvas state query index.html builds (currentStateQuery, lifted
   verbatim) for the state given as JSON on argv[2]:
   {"repo", "nodeCeiling", "nodeMax", "relCeiling", "relMax", "isolatedEntity", "isolatedRel"}.
   test_canvas_ceiling_live.py runs the printed query against Neo4j. */
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
  grab(/^function escapeCypherStr\(/m, "\n"),
  grab(/^let REGISTERED_REPO_IDS = /m, "\n"),
  grab(/^function allReposClause\(/m, "\n}"),
  grab(/^function currentStateQuery\(/m, "\n}"),
].join("\n");

const s = JSON.parse(process.argv[2]);
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
process.stdout.write(currentStateQuery());
