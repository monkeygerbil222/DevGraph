// CPU profile of an isolated harness page while panning after settle. Usage: node profpage.mjs <page> <ds> [query]
import puppeteer from "puppeteer-core";
const [pg, ds, extra = ""] = process.argv.slice(2);
const b = await puppeteer.launch({ executablePath: process.env.CHROME || "/usr/bin/google-chrome", headless: false, protocolTimeout: 600000, args: ["--headless=new","--use-angle=gl","--enable-gpu","--ignore-gpu-blocklist","--disable-gpu-vsync","--disable-frame-rate-limit","--window-size=1600,900"] });
const p = await b.newPage(); await p.setViewport({width:1600,height:900});
const cdp = await p.createCDPSession(); await cdp.send("Profiler.enable"); await cdp.send("Profiler.setSamplingInterval", { interval: 500 });
await p.goto(`http://127.0.0.1:8899/bench/pages/${pg}.html?ds=${ds}${extra}`);
await p.waitForFunction(() => window.__B && window.__B.marks.settled, { timeout: 400000, polling: 500 });
await new Promise(r => setTimeout(r, 1500));
await cdp.send("Profiler.start");
await p.evaluate(() => new Promise(res => { const t0 = performance.now(); (function f(now) { const el = now - t0; window.__B.pan(Math.cos(el/400)*6, Math.sin(el/400)*6); if (el < 6000) requestAnimationFrame(f); else res(); })(t0); }));
const { profile } = await cdp.send("Profiler.stop"); await b.close();
const byId = new Map(profile.nodes.map(n => [n.id, n])); const self = new Map(); let total = 0;
profile.samples.forEach((s, i) => { const f = byId.get(s).callFrame; const k = `${f.functionName || "(anon)"} ${f.url.split("/").pop()}:${f.lineNumber + 1}`; const d = profile.timeDeltas[i] || 0; total += d; self.set(k, (self.get(k) || 0) + d); });
console.log("total", (total/1000).toFixed(0), "ms"); [...self].sort((a,b)=>b[1]-a[1]).slice(0,18).forEach(([k,v]) => console.log((v/1000).toFixed(0).padStart(6), "ms", (100*v/total).toFixed(1).padStart(5)+"%", k));
