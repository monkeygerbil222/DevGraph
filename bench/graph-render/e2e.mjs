// Real-browser checks of the dashboard canvas: headless Chrome on the real
// page, served with the mock API (server.mjs), driven with real mouse
// events. Stub tests can't see what is drawn; these read the screen.
//   node e2e.mjs [check ...]     (default: every check; exit code 1 on a failure)
// Needs `npm install` here and the synthetic data (`npm run data`).
import puppeteer from "puppeteer-core";
import { spawn } from "node:child_process";
import fs from "node:fs";

const PORT = 8921, BASE = `http://127.0.0.1:${PORT}`;
const HERE = new URL(".", import.meta.url).pathname;
const server = spawn(process.execPath, [`${HERE}server.mjs`, String(PORT)], { stdio: "ignore" });
for (let i = 0; i < 50; i++) { try { await fetch(`${BASE}/api/repos`); break; } catch { await new Promise(r => setTimeout(r, 100)); } }

let failures = 0;
const check = (label, cond, detail) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${cond ? "" : "\n        " + detail}`);
  if (!cond) failures++;
};
async function open(ds = "synth2k", args = ["--use-angle=gl", "--enable-gpu", "--ignore-gpu-blocklist"]) {
  await fetch(`${BASE}/bench/ds/${ds}`);
  const browser = await puppeteer.launch({ executablePath: process.env.CHROME || "/usr/bin/google-chrome", headless: false, protocolTimeout: 900000,
    args: ["--headless=new", "--window-size=1600,900", "--no-first-run", `--user-data-dir=${HERE}chrome-prof-e2e-${process.pid}-${Date.now()}`, ...args] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 900 });
  const errors = []; page.on("pageerror", e => errors.push(String(e).slice(0, 300)));
  const requests = []; page.on("request", r => requests.push(r.method() + " " + r.url()));
  await page.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
  return { browser, page, errors, requests };
}
const settled = (page, ms = 240000) => page.waitForFunction(() => { try { return graphSettled && graphView.nodeCount() > 0; } catch { return false; } }, { timeout: ms, polling: 200 });
const sleep = ms => new Promise(r => setTimeout(r, ms));
/* RGBA pixels of a screen region, from a full-page screenshot decoded in the
   page (no image library here). Not a clipped screenshot: with headless
   Chrome a clip can come back as an older frame of the WebGL canvas, which
   shows neither a hover nor nodes drawn since. */
async function pixels(page, clip) {
  const b64 = await page.screenshot({ encoding: "base64" });
  return page.evaluate(async (b64, c) => {
    const img = new Image(); img.src = "data:image/png;base64," + b64; await img.decode();
    const cv = document.createElement("canvas"); cv.width = c.width; cv.height = c.height;
    const ctx = cv.getContext("2d"); ctx.drawImage(img, -c.x, -c.y);
    return Array.from(ctx.getImageData(0, 0, c.width, c.height).data);
  }, b64, clip);
}
const hexRgb = h => { const n = parseInt(h.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; };
/* the node farthest from the graph's middle, put mid-screen at zoom 2.5 */
async function loneNode(page) {
  const id = await page.evaluate(() => { const c = boundingCircle(graphView.nodeIds()); let best = null, bd = -1;
    graphView.forEachPosition((id, x, y) => { const d = Math.hypot(x - c.cx, y - c.cy); if (d > bd) { bd = d; best = id; } }); return best; });
  await page.evaluate(id => graphView.setView(graphView.position(id), 2.5), id);
  await sleep(800);
  const p = await page.evaluate(id => graphView.toScreen(graphView.position(id)), id);
  return { id, x: Math.round(p.x), y: Math.round(p.y) };
}

const checks = {
  /* A real pointer over a node brightens it and draws its name. */
  async hover() {
    const { browser, page, errors } = await open();
    await settled(page); await sleep(1500);
    const n = await loneNode(page);
    const cat = await page.evaluate(id => graphView.data(id, "cat"), n.id);
    const [bright, plain] = await page.evaluate(cat => [catColorBright(cat), catColor(cat)], cat);
    const brightRgb = bright.match(/\d+/g).map(Number), plainRgb = hexRgb(plain);
    const labelBox = { x: n.x + 12, y: n.y - 12, width: 160, height: 24 };
    const whiteIn = px => { let k = 0; for (let i = 0; i < px.length; i += 4) if (px[i] > 200 && px[i + 1] > 200 && px[i + 2] > 200) k++; return k; };
    const before = whiteIn(await pixels(page, labelBox));
    for (const d of [6, 4, 2, 1, 0]) { await page.mouse.move(n.x + d, n.y + d); await sleep(120); }
    await sleep(800);
    const centre = (await pixels(page, { x: n.x - 1, y: n.y - 1, width: 3, height: 3 })).slice(16, 19);
    const after = whiteIn(await pixels(page, labelBox));
    const near = (a, b) => a.every((v, i) => Math.abs(v - b[i]) < 30);
    check("hovering a node with the mouse draws its name", after > before + 15, JSON.stringify({ before, after }));
    check("...and brightens it", near(centre, brightRgb) && !near(centre, plainRgb), JSON.stringify({ centre, brightRgb, plainRgb }));
    await page.mouse.move(n.x + 200, n.y + 150, { steps: 5 }); await sleep(800);
    const gone = whiteIn(await pixels(page, labelBox));
    const centre2 = (await pixels(page, { x: n.x - 1, y: n.y - 1, width: 3, height: 3 })).slice(16, 19);
    check("leaving it takes the name and the brightness away", gone <= before + 3 && near(centre2, plainRgb), JSON.stringify({ gone, centre2 }));
    check("no page errors", !errors.length, errors.join(" | "));
    await browser.close();
  },
  /* Without WebGL 2 the canvas can't be drawn, but the rest of the
     dashboard (repositories, console, panels) must still work, and the
     canvas area says why it is empty. */
  async nowebgl() {
    const { browser, page, errors } = await open("synth2k", ["--disable-webgl", "--disable-3d-apis"]);
    await sleep(8000);
    const r = await page.evaluate(() => {
      const msg = document.getElementById("graphUnavailable");
      return {
        repos: [...document.getElementById("repoSelect").options].map(o => o.value),
        msg: msg && !msg.hidden ? msg.textContent : null,
        status: document.getElementById("neo4jStatus")?.textContent,
        query: document.getElementById("cypherInput").value,
        settled: (() => { try { return graphSettled; } catch { return "throws"; } })(),
      };
    });
    check("without WebGL the page runs: no uncaught errors", !errors.length, errors.join(" | "));
    check("...the repository list loads", r.repos.includes("zz-render-synth"), JSON.stringify(r.repos));
    check("...the canvas query still runs (console and counts)", /MATCH \(n\)/.test(r.query) && /connected/.test(r.status || ""), JSON.stringify(r));
    check("...and the canvas area says the graph needs WebGL 2", /WebGL 2/.test(r.msg || ""), JSON.stringify(r.msg));
    await browser.close();
  },
};

const wanted = process.argv.slice(2).length ? process.argv.slice(2) : Object.keys(checks);
try { for (const name of wanted) { console.log(`--- ${name}`); await checks[name](); } }
finally {
  server.kill();
  for (const d of fs.readdirSync(HERE)) if (d.startsWith(`chrome-prof-e2e-${process.pid}`)) fs.rmSync(HERE + d, { recursive: true, force: true });
}
console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
