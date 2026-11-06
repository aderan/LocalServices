#!/usr/bin/env node
/* D5 实机域名证据（真实 Chrome，禁代理直连回环）：
 * 1) buildbase.locove:5670 整页 + 资源经域名加载（Next.js 应用）
 * 2) 工作台设置页：模块状态=可用、端口 5670、预览 URL、分层诊断
 * 3) 主视图服务行：域名 chip 显示 host:5670，复制地址与 public_url 一致 */
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const OUT = process.argv[2] || "artifacts/acceptance-LOCOVE-FOUNDATION-DOMAINS-UI";
const DASH = process.env.LS_URL || "http://127.0.0.1:47800/";
const APP = process.env.LS_APP_URL || "http://buildbase.locove:5670/zh";
const PORT = 9341;

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function withPage(url, fn) {
  const profile = fs.mkdtempSync("/tmp/d5-chrome-");
  const chrome = spawn(CHROME, [
    "--headless=new", "--disable-gpu", "--no-proxy-server",
    `--remote-debugging-port=${PORT}`, "--window-size=1280,900",
    `--user-data-dir=${profile}`, "--no-first-run", "about:blank",
  ], {stdio: ["ignore", "ignore", "pipe"]});
  chrome.stderr.on("data", () => {});
  process.on("exit", () => { try { chrome.kill(); } catch (e) {} });
  let target = null;
  for (let i = 0; i < 50 && !target; i++) {
    await sleep(200);
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      target = list.find(t => t.type === "page");
    } catch (e) { /* retry */ }
  }
  if (!target) throw new Error("devtools target not found");
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let seq = 0;
  const pending = new Map();
  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  };
  const send = (method, params = {}) => new Promise((res, rej) => {
    const id = ++seq;
    pending.set(id, m => m.error ? rej(new Error(method + ": " + JSON.stringify(m.error))) : res(m.result));
    ws.send(JSON.stringify({id, method, params}));
  });
  const ev = async (expr, awaitPromise = false) => {
    const r = await send("Runtime.evaluate", {expression: expr, returnByValue: true, awaitPromise});
    if (r.exceptionDetails) throw new Error("eval: " + JSON.stringify(r.exceptionDetails).slice(0, 300));
    return r.result?.value;
  };
  const shot = async name => {
    const r = await send("Page.captureScreenshot", {format: "png"});
    fs.writeFileSync(path.join(OUT, name), Buffer.from(r.data, "base64"));
    console.log("SHOT", name);
  };
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Page.navigate", {url});
  await sleep(4500);
  try {
    return await fn({ev, shot, send});
  } finally {
    try { chrome.kill(); } catch (e) {}
    try { fs.rmSync(profile, {recursive: true, force: true}); } catch (e) {}
    await sleep(500);
  }
}

(async () => {
  const results = [];
  const check = (name, cond, detail) => { results.push(`${cond ? "PASS" : "FAIL"} ${name}${cond ? "" : " :: " + (detail ?? "")}`); };

  // 1) 业务应用经域名 :5670 整页加载（Next.js；资源同 Host）
  const app = await withPage(APP, async ({ev, shot}) => {
    const info = await ev(`(() => {
      const assets = Array.from(document.querySelectorAll('link[rel=stylesheet],script[src],img[src]'));
      return {title: document.title, ready: document.readyState,
              text: (document.body?.innerText || "").slice(0, 120),
              assets: assets.length,
              assetHosts: [...new Set(assets.map(a => { try { return new URL(a.href || a.src).host; } catch (e) { return "?"; } }))]};
    })()`);
    await shot("d5-buildbase-locove-5670.png");
    return info;
  });
  check("buildbase.locove:5670 页面加载（title 非空）", !!app.title, JSON.stringify(app).slice(0, 200));
  check("页面资源存在且同 Host 提供", app.assets > 0 && app.assetHosts.every(h => h === "buildbase.locove:5670"),
        JSON.stringify(app.assetHosts));

  // 2) 工作台设置页：模块状态/端口/预览/分层
  const settings = await withPage(DASH, async ({ev, shot}) => {
    await ev("setView('settings')");
    await sleep(600);
    const info = await ev(`(() => {
      const card = document.getElementById('locove-settings');
      return {state: document.getElementById('locove-state')?.textContent,
              cardText: card?.textContent || "",
              preview: card?.querySelector('code.mono')?.textContent,
              port: card?.querySelector('input[type=number]')?.value,
              dnsOk: !![...(document.querySelectorAll('#main table td'))].find(td => td.textContent.includes('本地 DNS')),
              legacyShown: card?.textContent.length > 0 && !!document.body.textContent.includes('旧 80')};
    })()`);
    await shot("d5-settings-domain-available.png");
    return info;
  });
  check("设置页模块状态=可用", settings.state === "可用", JSON.stringify(settings));
  check("设置页端口=5670", settings.port === "5670", settings.port);
  check("预览 URL 含 :5670", /:5670/.test(settings.preview || ""), settings.preview);
  check("影响说明含取消后直连语义", /取消后.{0,3}直连|取消后继续直连/.test(settings.cardText || ""), JSON.stringify(settings).slice(0,160));
  check("旧 80 残留在诊断中如实展示", settings.legacyShown);

  // 3) 主视图 chip：host:5670 且复制地址与 public_url 一致
  const main = await withPage(DASH, async ({ev, shot}) => {
    await sleep(3500);
    const info = await ev(`(() => {
      const chips = Array.from(document.querySelectorAll('.svc button.domain'));
      return {first: chips[0]?.textContent || "", title: chips[0]?.title || "", count: chips.length};
    })()`);
    await shot("d5-main-domain-chips-5670.png");
    return info;
  });
  check("服务行域名 chip 显示 host:5670", /:5670/.test(main.first), JSON.stringify(main));
  check("chip 复制地址（title）与 :5670 URL 一致", /^http:\/\/[^/]+:5670/.test(main.title), main.title);

  console.log("\n===== D5 域名实机验证 =====");
  results.forEach(r => console.log(r));
  const fails = results.filter(r => r.startsWith("FAIL")).length;
  fs.writeFileSync(path.join(OUT, "d5-domain-evidence.txt"),
    `${new Date().toISOString()}\nAPP ${APP}\nDASH ${DASH}\n` + results.join("\n") + `\n${fails} failed\n`);
  process.exit(fails ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
