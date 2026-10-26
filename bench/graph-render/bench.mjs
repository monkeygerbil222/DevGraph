// Render benchmark driver: one fresh headless Chrome (real GPU via ANGLE/GL)
// per (renderer, dataset). Usage: node bench.mjs <renderer> <dataset> [outJson]
// renderers: current | v334 | webgl (the real dashboard page, mocked API)
//            cyweb | cycanvas (Cytoscape 3.34 with the dashboard style, worker layout;
//              WebGL / canvas) | sigma | cosmos | d3canvas | forcegraph (isolated pages)
import puppeteer from "puppeteer-core";
import fs from "node:fs";

const [renderer, ds, outFile] = process.argv.slice(2);
const BASE = "http://127.0.0.1:8899";
const REAL = { current: "", v334: "v334", webgl: "webgl" };
const W = 1600, H = 900;
const SCRATCH = new URL(".", import.meta.url).pathname;
fs.mkdirSync(`${SCRATCH}shots`, { recursive: true });

const browser = await puppeteer.launch({
  executablePath: process.env.CHROME || "/usr/bin/google-chrome", headless: false, protocolTimeout: 600000,
  args: ["--headless=new", "--use-angle=gl", "--enable-gpu", "--ignore-gpu-blocklist", ...(process.env.VSYNC ? [] : ["--disable-gpu-vsync", "--disable-frame-rate-limit"]),
    "--enable-precise-memory-info", `--window-size=${W},${H}`, "--no-first-run", `--user-data-dir=${SCRATCH}chrome-prof-${process.pid}`],
});
const result = { renderer, ds };
try {
  const page = await browser.newPage();
  await page.setViewport({ width: W, height: H });
  const errors = []; page.on("pageerror", e => errors.push(String(e).slice(0, 200)));
  page.on("console", m => { if (m.type() === "error") errors.push(m.text().slice(0, 200)); });
  const cdp = await page.createCDPSession();
  await cdp.send("Performance.enable");
  // frame + long-task recorders, installed before any page script
  await page.evaluateOnNewDocument(() => {
    console.warn = console.log = console.trace = console.info = () => {}; // devtools-protocol console serialisation would otherwise dominate cytoscape's per-element style warnings
    window.__frames = []; (function f(t) { window.__frames.push(t); requestAnimationFrame(f); })(performance.now());
    window.__long = []; try { new PerformanceObserver(l => l.getEntries().forEach(e => window.__long.push([e.startTime, e.duration]))).observe({ entryTypes: ["longtask"] }); } catch {}
  });
  const real = renderer in REAL;
  if (real) {
    await (await fetch(`${BASE}/bench/ds/${ds}`)).json();
    await page.evaluateOnNewDocument(() => {
      window.__B = { marks: {}, info: {} };
      const mark = k => { if (window.__B.marks[k] == null) window.__B.marks[k] = performance.now(); };
      const of = window.fetch;
      window.fetch = async function (u, o) {
        const isGraph = o && typeof o.body === "string" && o.body.includes("AS matchedId") && o.body.includes("RETURN n, r, m");
        if (isGraph) mark("fetchStart");
        const r = await of.apply(this, arguments);
        if (isGraph) { const oj = r.json.bind(r); r.json = async () => { const j = await oj(); mark("data"); return j; }; }
        return r;
      };
      // the page's canvas: graphView (sigma) since the rework, cy (Cytoscape) before it
      const iv = setInterval(() => {
        let v = null, c = null;
        try { v = graphView; } catch { try { c = cy; } catch { return; } } // eslint-disable-line no-undef
        if (v ? typeof v.nodeCount !== "function" : (!c || typeof c.nodes !== "function")) return;
        clearInterval(iv);
        const om = window.mapGraphResultToElements;
        window.mapGraphResultToElements = function () { const t = performance.now(); const r = om.apply(this, arguments); window.__B.info.mapMs = (window.__B.info.mapMs || 0) + performance.now() - t; return r; };
        const omg = window.mergeGraphElements;
        window.mergeGraphElements = function () { const t = performance.now(); const r = omg.apply(this, arguments); window.__B.info.mergeMs = (window.__B.info.mergeMs || 0) + performance.now() - t; requestAnimationFrame(() => requestAnimationFrame(() => mark("first"))); return r; };
        const s = setInterval(() => { let gs; try { gs = graphSettled; } catch { return; } if (window.__B.marks.first && gs) { mark("settled"); clearInterval(s); } }, 50); // eslint-disable-line no-undef
        if (v) {
          window.__B.pan = (dx, dy) => { const { width, height } = v.size(); v.setView(v.toModel({ x: width / 2 - dx, y: height / 2 - dy })); };
          window.__B.getZoom = () => v.zoom();
          window.__B.zoomTo = z => v.setView(null, z);
          window.__B.nodeCount = () => v.nodeCount();
          window.__B.allScreen = () => { const out = []; v.forEachPosition((id, x, y) => { const p = v.toScreen({ x, y }); out.push(p.x, p.y); }); return out; };
          let ids = null;
          window.__B.nodeScreen = i => { ids = ids || v.nodeIds(); const p = v.toScreen(v.position(ids[i])); return [p.x, p.y]; };
          // the node under the pointer, as the page's hover handler saw it (nodes overlap in a dense view)
          window.__B.hovered = () => { ids = ids || v.nodeIds(); let h = -1; v.forEachNode((id, a) => { if (a.hoverHl) h = ids.indexOf(id); }); return h; };
          Object.defineProperty(window.__B, "n", { get: () => v.nodeCount() });
        } else {
          window.__B.pan = (dx, dy) => c.panBy({ x: dx, y: dy });
          window.__B.getZoom = () => c.zoom();
          window.__B.zoomTo = z => c.zoom({ level: z, renderedPosition: { x: c.width() / 2, y: c.height() / 2 } });
          window.__B.nodeCount = () => c.nodes().length;
          window.__B.allScreen = () => { const out = []; c.nodes().forEach(n => { const p = n.renderedPosition(); out.push(p.x, p.y); }); return out; };
        }
      }, 5);
    });
    await page.goto(`${BASE}/?variant=${REAL[renderer]}`, { waitUntil: "domcontentloaded" });
  } else {
    const pageName = renderer === "cycanvas" ? "cyweb" : renderer;
    await page.goto(`${BASE}/bench/pages/${pageName}.html?ds=${ds}${renderer === "cycanvas" ? "&webgl=0" : ""}`, { waitUntil: "domcontentloaded" });
  }
  const waitMark = async (k, ms) => { try { await page.waitForFunction(k => window.__B && window.__B.marks[k] != null, { timeout: ms, polling: 200 }, k); return true; } catch { return false; } };
  result.firstOk = await waitMark("first", 240000);
  result.settledOk = result.firstOk && await waitMark("settled", 240000);
  await new Promise(r => setTimeout(r, 2500)); // let any post-settle fit animation finish
  const m = await page.evaluate(() => {
    const B = window.__B, f = window.__frames, a = B.marks.data ?? B.marks.first, b = B.marks.settled ?? performance.now();
    const fr = f.filter(t => t >= a && t <= b), gaps = fr.slice(1).map((t, i) => t - fr[i]).sort((x, y) => x - y);
    const longMs = window.__long.filter(([s]) => s >= (B.marks.fetchStart ?? 0) && s <= b).reduce((s, [, d]) => s + d, 0);
    return { marks: B.marks, info: B.info, nodes: B.nodeCount ? B.nodeCount() : B.n,
      settleFps: gaps.length ? gaps.length * 1000 / (b - a) : null, settleMaxFrameMs: gaps.length ? gaps[gaps.length - 1] : null, longTaskMsToSettle: longMs };
  });
  Object.assign(result, m);
  await cdp.send("HeapProfiler.collectGarbage");
  const metrics = (await cdp.send("Performance.getMetrics")).metrics;
  result.jsHeapMB = +(metrics.find(x => x.name === "JSHeapUsedSize").value / 1048576).toFixed(1);
  await page.screenshot({ path: `${SCRATCH}shots/${renderer}-${ds}.png` });
  const measure = (kind, ms) => page.evaluate((kind, ms) => new Promise(res => {
    const B = window.__B, z0 = B.getZoom(); const iv = []; let t0, last;
    function f(now) {
      if (t0 == null) { t0 = last = now; } else { iv.push(now - last); last = now; }
      const el = now - t0;
      if (kind === "pan") B.pan(Math.cos(el / 400) * 6, Math.sin(el / 400) * 6);
      if (kind === "zoom") B.zoomTo(z0 * Math.exp(0.7 * Math.sin(el / 500)));
      if (el < ms) requestAnimationFrame(f);
      else { if (kind === "zoom") B.zoomTo(z0); iv.sort((x, y) => x - y); res({ fps: +(iv.length * 1000 / el).toFixed(1), p95: +iv[Math.floor(iv.length * 0.95)].toFixed(1), max: +iv[iv.length - 1].toFixed(1) }); }
    }
    requestAnimationFrame(f);
  }), kind, ms);
  result.panApi = await measure("pan", 4000);
  // pan the way a user does: a mouse drag starting on empty background
  const empty = await page.evaluate((W, H) => {
    const B = window.__B; let xy = B.allScreen ? B.allScreen() : null;
    if (!xy) { xy = []; for (let i = 0; i < B.n; i++) xy.push(...B.nodeScreen(i)); }
    const C = 24, occ = new Set(); for (let i = 0; i < xy.length; i += 2) occ.add(Math.floor(xy[i] / C) + "," + Math.floor(xy[i + 1] / C));
    let best = null, bd = Infinity;
    for (let gx = 6; gx < W / C - 6; gx++) for (let gy = 6; gy < H / C - 6; gy++) {
      let free = true; for (let a = -2; a <= 2 && free; a++) for (let b = -2; b <= 2; b++) if (occ.has((gx + a) + "," + (gy + b))) { free = false; break; }
      if (!free) continue; const d = Math.hypot(gx * C - W / 2, gy * C - H / 2); if (d < bd) { bd = d; best = [gx * C + C / 2, gy * C + C / 2]; }
    }
    return best;
  }, W, H);
  if (empty) {
    await page.evaluate(() => { window.__p0 = window.__frames.length; window.__pT = performance.now(); });
    await page.mouse.move(empty[0], empty[1]); await page.mouse.down();
    const t0 = Date.now(); let k = 0;
    while (Date.now() - t0 < 4000) { k++; await page.mouse.move(empty[0] + Math.cos(k / 10) * 100, empty[1] + Math.sin(k / 10) * 100); await new Promise(r => setTimeout(r, 8)); }
    await page.mouse.up();
    result.pan = await page.evaluate(() => { const f = window.__frames.slice(window.__p0); const el = performance.now() - window.__pT; const g = f.slice(1).map((t, i) => t - f[i]).sort((a, b) => a - b); return { fps: +(g.length * 1000 / el).toFixed(1), p95: +(g[Math.floor(g.length * 0.95)] || 0).toFixed(1), max: +(g[g.length - 1] || 0).toFixed(1) }; });
    result.pan.moves = k;
  } else result.pan = null;
  result.zoom = await measure("zoom", 4000);
  if (await page.evaluate(() => typeof window.__B.nodeScreen === "function")) {
    // node drag through real mouse events on a node near the centre (the
    // Cytoscape page had node drag off)
    const pick = await page.evaluate((W, H) => {
      const B = window.__B; let best = -1, bd = Infinity;
      for (let k = 0; k < 400; k++) { const i = Math.floor(Math.random() * B.n); const [x, y] = B.nodeScreen(i); const dd = Math.hypot(x - W / 2, y - H / 2); if (dd < bd) { bd = dd; best = i; } }
      return { i: best, p: B.nodeScreen(best) };
    }, W, H);
    await page.mouse.move(pick.p[0] - 3, pick.p[1] - 3); await page.mouse.move(pick.p[0], pick.p[1], { steps: 3 });
    await new Promise(r => setTimeout(r, 200));
    const hovered = await page.evaluate(() => window.__B.hovered ? window.__B.hovered() : -1);
    if (hovered >= 0) pick.i = hovered; // follow the node actually grabbed
    await page.evaluate(() => { window.__dragFrames0 = window.__frames.length; window.__dragT0 = performance.now(); });
    await page.mouse.down();
    const t0 = Date.now(); let k = 0;
    // a 120 px circle that starts at the grab point, so the pointer never jumps
    // away from the node (sigma only starts a drag after a few small moves)
    const at = k => [pick.p[0] + (Math.cos(k / 8) - 1) * 120, pick.p[1] + Math.sin(k / 8) * 120];
    while (Date.now() - t0 < 4000) { k++; await page.mouse.move(...at(k), { steps: 1 }); await new Promise(r => setTimeout(r, 8)); }
    const after = await page.evaluate(i => window.__B.nodeScreen(i), pick.i);
    const target = at(k);
    await page.mouse.up();
    result.drag = await page.evaluate(() => { const f = window.__frames.slice(window.__dragFrames0); const el = performance.now() - window.__dragT0; const g = f.slice(1).map((t, i) => t - f[i]).sort((a, b) => a - b); return { fps: +(g.length * 1000 / el).toFixed(1), p95: +(g[Math.floor(g.length * 0.95)] || 0).toFixed(1) }; });
    result.drag.nodeFollowedPx = +Math.hypot(after[0] - target[0], after[1] - target[1]).toFixed(0);
    result.drag.grabbed = hovered >= 0;
  }
  result.errors = errors.slice(0, 5);
} catch (e) { result.fatal = String(e).slice(0, 300); }
await browser.close();
fs.rmSync(`${SCRATCH}chrome-prof-${process.pid}`, { recursive: true, force: true });
const line = JSON.stringify(result);
console.log(line);
if (outFile) fs.appendFileSync(outFile, line + "\n");
