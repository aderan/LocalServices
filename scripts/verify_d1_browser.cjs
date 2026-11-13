#!/usr/bin/env node
/* D1 真实浏览器证据（review 2026-11-05 UX1/UX2）：
 * UX1 地址 chip：Enter/Space 触发复制语义（不被父行吞掉 preventDefault/展开），
 *              点击 chip 不冒泡展开行；行自身键盘展开仍可用。
 * UX2 日志阅读：悬停/持有选区时 3s 轮询不重建内容区（节点身份保持），释放后恢复。 */
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const OUT = process.argv[2] || "artifacts/acceptance-LOCOVE-FOUNDATION-DOMAINS-UI";
const URL = process.env.LS_URL || "http://127.0.0.1:47800/";
const PORT = 9337;

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function main() {
  const profile = fs.mkdtempSync("/tmp/d1-chrome-");
  const chrome = spawn(CHROME, [
    "--headless=new", "--disable-gpu", "--no-proxy-server",
    `--remote-debugging-port=${PORT}`, "--window-size=1280,900",
    `--user-data-dir=${profile}`, "--no-first-run", "about:blank",
  ], {stdio: ["ignore", "ignore", "pipe"]});
  chrome.stderr.on("data", () => {});
  const cleanup = () => { try { chrome.kill(); } catch (e) {} try { fs.rmSync(profile, {recursive: true, force: true}); } catch (e) {} };
  process.on("exit", cleanup);

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
  await sleep(3500);

  check("真实页面加载", await ev("document.querySelectorAll('.svc').length") > 0,
        await ev("document.querySelectorAll('.svc').length"));

  const reasonShown = await ev(`(() => {
    const original = snap;
    const originalView = view;
    const reason = "必要依赖未就绪：Colima 恢复失败";
    const failed = {id: "diagnosis-fixture", name: "ShiCha", state: "failed", label: "故障", reason};
    try {
      snap = {...snap, projects: [failed]};
      setView("attention");
      const button = document.querySelector('#p-diagnosis-fixture [data-act="reason"]');
      button.click();
      const detail = document.querySelector("#p-diagnosis-fixture .detail");
      return !!detail?.textContent.includes(reason) &&
        detail.contains(document.activeElement) && document.activeElement.textContent.includes(reason);
    } finally {
      snap = original;
      setView(originalView);
    }
  })()`);
  check("故障服务的「查看原因」展开详情并聚焦诊断", reasonShown);

  const navigation = await ev(`(() => {
    const previous = {snap, registry, view};
    const services = [
      {id: "ready", name: "Ready", state: "available"},
      {id: "off", name: "Off", state: "stopped"},
      {id: "degraded", name: "Degraded", state: "partial"},
      {id: "blocked", name: "Blocked", state: "conflict"},
    ];
    try {
      snap = {...snap, projects: services};
      registry = {registry: {projects: [
        {id: "healthy", name: "正常项目", services: ["ready", "off"]},
        {id: "partial", name: "部分项目", services: ["degraded"]},
        {id: "failure", name: "冲突项目", services: ["degraded", "blocked"]},
      ]}};
      setView("all");
      const projects = [...document.querySelectorAll("#navProjects button")];
      const find = name => projects.find(button => button.textContent.trim() === name);
      const settings = document.getElementById("nav-settings");
      const inSidebar = !!settings?.closest("nav");
      settings?.click();
      return {
        healthy: !!find("正常项目") && !find("正常项目").querySelector(".project-alert"),
        partial: find("部分项目")?.getAttribute("aria-label") === "部分项目，" + tr("部分可用", "Degraded") &&
          !!find("部分项目").querySelector(".project-alert.partial"),
        failure: find("冲突项目")?.getAttribute("aria-label") === "冲突项目，" + tr("需要处理", "Needs attention") &&
          !!find("冲突项目").querySelector(".project-alert:not(.partial)"),
        attentionCount: document.getElementById("attentionBadge").textContent,
        settings: inSidebar && document.getElementById("viewTitle").textContent === tr("设置", "Settings") &&
          settings.classList.contains("active") && settings.getAttribute("aria-current") === "page",
      };
    } finally {
      snap = previous.snap;
      registry = previous.registry;
      setView(previous.view);
    }
  })()`);
  check("项目侧栏只标记异常并保留全局待处理数", navigation.healthy &&
        navigation.partial && navigation.failure && navigation.attentionCount === "2", JSON.stringify(navigation));
  check("侧栏设置入口可打开设置并标记当前页", navigation.settings);

  // ---------------- UX1：地址 chip 的完整独立事件语义 ----------------
  const chipExists = await ev("!!document.querySelector('.svc .line button.domain')");
  check("地址 chip 存在于服务行", chipExists);
  if (chipExists) {
    // 键盘 Enter：不被父行 preventDefault / 不触发展开
    const enter = await ev(`(() => {
      const chip = document.querySelector('.svc .line button.domain');
      const line = chip.closest('.line');
      const before = line.getAttribute('aria-expanded');
      chip.focus();
      const kd = new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true});
      chip.dispatchEvent(kd);
      return {defaultPrevented: kd.defaultPrevented, before,
              after: line.getAttribute('aria-expanded'), focused: document.activeElement === chip};
    })()`);
    check("UX1 Enter 聚焦 chip：事件不被父行吞掉（无 preventDefault）", enter.focused && !enter.defaultPrevented,
          JSON.stringify(enter));
    check("UX1 Enter 聚焦 chip：不触发行展开", enter.after === enter.before, JSON.stringify(enter));

    const space = await ev(`(() => {
      const chip = document.querySelector('.svc .line button.domain');
      const line = chip.closest('.line');
      chip.focus();
      const kd = new KeyboardEvent('keydown', {key: ' ', bubbles: true, cancelable: true});
      chip.dispatchEvent(kd);
      return {defaultPrevented: kd.defaultPrevented, expanded: line.getAttribute('aria-expanded')};
    })()`);
    check("UX1 Space 聚焦 chip：无 preventDefault 且不展开", !space.defaultPrevented && space.expanded === "false",
          JSON.stringify(space));

    const click = await ev(`(() => {
      const chip = document.querySelector('.svc .line button.domain');
      const line = chip.closest('.line');
      chip.click();
      return {expanded: line.getAttribute('aria-expanded'), sectionExpanded:
              chip.closest('.svc').classList.contains('expanded')};
    })()`);
    check("UX1 点击 chip 复制：不冒泡展开行", !click.sectionExpanded, JSON.stringify(click));

    // 回归：行自身聚焦时 Enter 仍展开（断言查当前 DOM——toggleDetail 会整树重建）
    const rowKey = await ev(`(() => {
      const line = document.querySelector('.svc .line');
      line.focus();
      const kd = new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true});
      line.dispatchEvent(kd);
      return {defaultPrevented: kd.defaultPrevented,
              expanded: !!document.querySelector('.svc.expanded')};
    })()`);
    check("回归：行自身键盘 Enter 仍展开详情", rowKey.defaultPrevented && rowKey.expanded,
          JSON.stringify(rowKey));
    await ev("document.querySelector('.svc .line').click()");  // 收起，保持后续用例干净
    await sleep(300);
  }

  // ---------------- UX2：日志阅读/选择不被轮询打断 ----------------
  const logsReady = await ev(`(async () => {
    const row = document.querySelector('.svc .line');
    const id = row.closest('.svc').id.slice(2);
    await loadLogs(id);
    return !!document.querySelector('.svc.expanded .detail pre');
  })()`, true);
  check("日志已加载到详情区", logsReady);

  if (logsReady) {
    const selHeld = await ev(`(() => {
      const pre = document.querySelector('.detail pre');
      window.__d1pre = pre;
      const range = document.createRange();
      range.selectNodeContents(pre);
      const sel = getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      return {reading: logReading(), hasSelection: !sel.isCollapsed};
    })()`);
    check("UX2 持有日志选区时 logReading()=true", selHeld.reading && selHeld.hasSelection,
          JSON.stringify(selHeld));
    await shot("d1-ux2-log-selection-held.png");

    await sleep(3700);  // 跨过至少一个 3s 轮询周期
    const kept = await ev(`window.__d1pre.isConnected && document.querySelector('.detail pre') === window.__d1pre`);
    check("UX2 选区持有期间日志节点不被轮询重建", kept);

    const released = await ev(`(() => {
      getSelection().removeAllRanges();
      return logReading();
    })()`);
    check("UX2 释放选区后 logReading()=false", released === false);
    await sleep(3700);
    const rebuilt = await ev(`!window.__d1pre.isConnected`);
    check("UX2 释放后轮询恢复刷新（旧节点被替换）", rebuilt);

    // 悬停保护：CDP 真实鼠标移动到日志区
    const box = await ev(`(() => {
      const r = document.querySelector('.detail pre').getBoundingClientRect();
      return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + Math.min(40, r.height / 2))};
    })()`);
    await send("Input.dispatchMouseEvent", {type: "mouseMoved", x: box.x, y: box.y});
    await sleep(200);
    const hoverTrue = await ev("logReading()");
    check("UX2 鼠标悬停日志区 logReading()=true", hoverTrue === true);
    await send("Input.dispatchMouseEvent", {type: "mouseMoved", x: 5, y: 5});
    await sleep(200);
    const hoverFalse = await ev("logReading()");
    check("UX2 鼠标移开日志区 logReading()=false", hoverFalse === false);
    await shot("d1-ux2-after-resume.png");
  }

  console.log("\n===== D1 浏览器验证 =====");
  results.forEach(r => console.log(r));
  const fails = results.filter(r => r.startsWith("FAIL")).length;
  fs.writeFileSync(path.join(OUT, "d1-browser-evidence.txt"),
    `${new Date().toISOString()}\nURL ${URL}\n` + results.join("\n") + `\n${fails} failed\n`);
  process.exit(fails ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(1); });
