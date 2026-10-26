// CPU profile of the real dashboard page (mocked API) during load + settle,
// or during a pan once settled. Usage: node profile.mjs <variant:current|webgl> <dataset> <phase:settle|pan> [seconds]
import puppeteer from "puppeteer-core";
import fs from "node:fs";
const [variant, ds, phase, secs = "12"] = process.argv.slice(2);
const BASE = "http://127.0.0.1:8899";
const SCRATCH = new URL(".", import.meta.url).pathname;
await (await fetch(`${BASE}/bench/ds/${ds}`)).json();
const browser = await puppeteer.launch({ executablePath: process.env.CHROME || "/usr/bin/google-chrome", headless: false, protocolTimeout: 600000,
  args: ["--headless=new", "--use-angle=gl", "--enable-gpu", "--ignore-gpu-blocklist", "--disable-gpu-vsync", "--disable-frame-rate-limit", "--window-size=1600,900", `--user-data-dir=${SCRATCH}chrome-prof-p${process.pid}`] });
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 900 });
const cdp = await page.createCDPSession();
await cdp.send("Profiler.enable"); await cdp.send("Profiler.setSamplingInterval", { interval: 500 });
await page.evaluateOnNewDocument(() => { console.warn = console.log = console.trace = () => {}; });
await page.evaluateOnNewDocument(() => { window.__settledFlag = () => { try { return graphSettled; } catch { return false; } }; }); // eslint-disable-line no-undef
await page.goto(`${BASE}/?variant=${variant === "current" ? "" : variant}`, { waitUntil: "domcontentloaded" });
if (phase === "settle") {
  await cdp.send("Profiler.start");
  await new Promise(r => setTimeout(r, +secs * 1000));
} else {
  await page.waitForFunction(() => window.__settledFlag() && typeof cy !== "undefined" && cy.nodes && cy.nodes().length > 10, { timeout: 400000, polling: 500 }); // eslint-disable-line no-undef
  await new Promise(r => setTimeout(r, 2500));
  await cdp.send("Profiler.start");
  await page.evaluate(secs => new Promise(res => { const t0 = performance.now(); (function f(now) { const el = now - t0; cy.panBy({ x: Math.cos(el / 400) * 6, y: Math.sin(el / 400) * 6 }); if (el < secs * 1000) requestAnimationFrame(f); else res(); })(t0); }), +secs); // eslint-disable-line no-undef
}
const { profile } = await cdp.send("Profiler.stop");
await browser.close();
fs.rmSync(`${SCRATCH}chrome-prof-p${process.pid}`, { recursive: true, force: true });
// self time per function, and inclusive time for a few page functions of interest
const byId = new Map(profile.nodes.map(n => [n.id, n]));
const self = new Map();
const dt = profile.timeDeltas; const counts = new Map();
profile.samples.forEach((s, i) => counts.set(s, (counts.get(s) || 0) + (dt[i] || 0)));
const total = [...counts.values()].reduce((a, b) => a + b, 0);
const parent = new Map(); profile.nodes.forEach(n => (n.children || []).forEach(c => parent.set(c, n.id)));
const incl = new Map();
for (const [id, us] of counts) {
  const n = byId.get(id); const f = n.callFrame; const key = `${f.functionName || "(anon)"} ${f.url.split("/").pop()}:${f.lineNumber + 1}`;
  self.set(key, (self.get(key) || 0) + us);
  const seen = new Set(); let cur = id;
  while (cur != null) { const cf = byId.get(cur).callFrame; const k = `${cf.functionName || "(anon)"} ${cf.url.split("/").pop()}:${cf.lineNumber + 1}`; if (!seen.has(k)) { seen.add(k); incl.set(k, (incl.get(k) || 0) + us); } cur = parent.get(cur); }
}
const top = (m, n) => [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, n).map(([k, v]) => `${(v / 1000).toFixed(0).padStart(7)} ms ${(100 * v / total).toFixed(1).padStart(5)}%  ${k}`).join("\n");
console.log(`total sampled ${(total / 1000).toFixed(0)} ms\n-- self --\n${top(self, 25)}\n-- inclusive (page functions) --\n${top(new Map([...incl].filter(([k]) => /index|\(anon\) :|^(step|frame|forceDirectedSettle|updateViewportCulling|mergeGraphElements|renderGlow|mapGraphResultToElements|animateGlowTo|rotationTick|quadRepulsion|buildQuadTree)/.test(k))), 30)}`);
