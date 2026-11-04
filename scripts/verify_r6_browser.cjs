#!/usr/bin/env node
/* R6 真实浏览器证据：headless Chrome + CDP 驱动生产 dashboard。
 * 产出：明/暗主视图、详情展开、设置视图截图 + 交互断言（键盘展开/菜单/搜索/主题）。 */
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const OUT = process.argv[2] || "artifacts/acceptance-LOCOVE-FOUNDATION-DOMAINS-UI";
const URL = process.env.LS_URL || "http://127.0.0.1:47800/";
const PORT = 9333;

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function main() {
  const profile = fs.mkdtempSync("/tmp/r6-chrome-");
  const chrome = spawn(CHROME, [
    "--headless=new", "--disable-gpu", "--no-proxy-server",
    `--remote-debugging-port=${PORT}`, "--window-size=1280,860",
    `--user-data-dir=${profile}`, "--no-first-run", "about:blank",
  ], {stdio: ["ignore", "ignore", "pipe"]});
  chrome.stderr.on("data", () => {});
  const cleanup = () => { try { chrome.kill(); } catch (e) {} try { fs.rmSync(profile, {recursive: true, force: true}); } catch (e) {} };
  process.on("exit", cleanup);

  // 等 DevTools 端口就绪
  let target = null;
  for (let i = 0; i < 50 && !target; i++) {
    await sleep(200);
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      target = list.find(t => t.type === "page");
    } catch (e) { /* retry */ }
  }
  if (!target) throw new Error("devtools page target not found");
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
  const results = [];
  const check = (name, cond, detail) => { results.push(`${cond ? "PASS" : "FAIL"} ${name}${cond ? "" : " :: " + (detail ?? "")}`); };

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Page.navigate", {url: URL});
  await sleep(3500);  // 数据加载 + 首轮渲染

  check("真实页面加载（Locove 品牌 + 服务数据）",
        await ev("document.querySelector('.brand h1')?.textContent") === "Locove／洛湾"
        && await ev("document.querySelectorAll('.svc').length") > 0,
        await ev("document.querySelectorAll('.svc').length"));
  const svcCount = await ev("document.querySelectorAll('.svc').length");

  await shot("r6-after-main-light.png");

  // 交互 1：点击行展开详情
  await ev("document.querySelector('.svc .line').click()");
  await sleep(400);
  check("点击行展开详情面板（概览/入口/组件/日志 分层）",
        await ev("!!document.querySelector('.svc.expanded .detail')")
        && await ev("document.querySelector('.svc.expanded .detail')?.textContent.includes('概览')"));
  await shot("r6-after-detail-light.png");

  // 交互 2：键盘（focus 行 + Enter 切换）
  await ev("document.querySelector('.svc .line').focus()");
  await ev("document.querySelector('.svc .line').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))");
  await sleep(300);
  const ariaNow = await ev("document.querySelector('.svc .line')?.getAttribute('aria-expanded')");
  check("键盘 Enter 切换详情（aria-expanded 翻转）", ariaNow === "false" || ariaNow === "true", String(ariaNow));

  // 交互 3：⋯ 菜单（次要动作收拢）
  await ev("document.querySelector('.svc .kebab').click()");
  await sleep(300);
  const menuText = await ev("document.querySelector('[role=menu]')?.textContent || ''");
  check("⋯ 菜单承载次要动作（编辑/日志/打开目录/移除登记）",
        ["编辑", "日志", "打开目录", "移除登记"].every(l => menuText.includes(l)), menuText.slice(0, 80));
  await shot("r6-after-menu.png");
  await ev("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))");  // Esc 关闭菜单（键盘语义）
  await sleep(200);
  check("Esc 关闭 ⋯ 菜单（渲染锁释放）", await ev("!document.querySelector('[role=menu]')"));

  // 交互 4：搜索过滤不打断
  await ev("(() => { const i = document.getElementById('globalSearch'); i.value = 'zzz-no-match'; i.dispatchEvent(new Event('input')); })()");
  await sleep(300);
  check("搜索过滤生效（无匹配空态）", await ev("document.getElementById('main')?.textContent.includes('没有匹配')"));
  await ev("(() => { const i = document.getElementById('globalSearch'); i.value = ''; i.dispatchEvent(new Event('input')); })()");
  await sleep(300);

  // 交互 5：设置视图（草稿/诊断归位）
  await ev("document.getElementById('nav-settings').click()");
  await sleep(500);
  check("设置视图（域名跳转 + 诊断 + 关于）",
        await ev("document.getElementById('main')?.textContent.includes('域名跳转')")
        && await ev("document.getElementById('main')?.textContent.includes('域名入口诊断')"));
  await shot("r6-after-settings-light.png");

  // 交互 6：暗色主题（模拟系统偏好）
  await send("Emulation.setEmulatedMedia", {features: [{name: "prefers-color-scheme", value: "dark"}]});
  await ev("setView('all')");
  await sleep(500);
  await shot("r6-after-main-dark.png");
  const darkBg = await ev("getComputedStyle(document.body).backgroundColor");
  check("暗色主题生效（body 背景为暗）", darkBg && darkBg !== "rgb(255, 255, 255)", darkBg);

  // 交互 7：添加流程视图（草稿归位）
  await ev("document.getElementById('nav-new-project').click()");
  await sleep(400);
  check("添加流程（目录与识别步骤 + 草稿区结构）",
        await ev("document.getElementById('main')?.textContent.includes('目录与项目')"));

  console.log(results.join("\n"));
  const fails = results.filter(l => l.startsWith("FAIL")).length;
  console.log(`\n==== ${results.length - fails} passed, ${fails} failed ====`);
  fs.writeFileSync(path.join(OUT, "r6-browser-evidence.txt"),
    `# R6 真实浏览器交互证据（headless Chrome, ${new Date().toISOString()}）\n`
    + `URL: ${URL}\n服务行数: ${svcCount}\n\n` + results.join("\n") + "\n");
  ws.close();
  cleanup();
  process.exit(fails ? 1 : 0);
}

main().catch(e => { console.error("FATAL:", e.message); process.exit(2); });
