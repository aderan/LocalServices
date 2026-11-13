#!/usr/bin/env node
/* W1 共享工作台（真实 Chromium 证据，LOCOVE-WORKBENCH-UI）：
 *  - 960×600（主窗口最小尺寸）与常用宽窗两档：服务行各列（长名/地址/状态/操作）
 *    互不覆盖、无横向溢出；设置长诊断在窄内容区单列可读。
 *  - 轮询稳定性：html 未变时节点身份保持；状态变化重建后焦点/滚动按锚点恢复；
 *    展开态跨真实 3s 轮询保持。
 *  - 侧栏项目项无常驻数字（异常才有点提示）；搜索/刷新/设置顶部可达。
 * 用法：node scripts/verify_w1_browser.cjs [证据目录]
 * 环境：LS_URL 指向工作台入口（默认真实 daemon 47800；只读断言，注入态均 finally 还原）。 */
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const OUT = process.argv[2] || "artifacts/acceptance-LOCOVE-WORKBENCH-UI";
const URL = process.env.LS_URL || "http://127.0.0.1:47800/";
const PORT = 9338;

const sleep = ms => new Promise(r => setTimeout(r, ms));
const results = [];
const check = (name, cond, detail) => { results.push(`${cond ? "PASS" : "FAIL"} ${name}${cond ? "" : " :: " + (detail ?? "")}`); };

// 页内注入的长名/长地址夹具（状态注入，finally 还原真实 snap）
const FIXTURE_EV = `(() => {
  const long = "很长很长很长很长很长很长很长很长很长很长的服务名OutOfBandAnalyzer";
  const p = {id: "w1-long-fixture", name: long, state: "available", label: "可用", intent: "running",
             can_open: true, description: "长地址与长名互不覆盖夹具", group: null, components: [],
             endpoints: [{host: "w1-long-fixture.locove", public_url: "http://127.0.0.1:61999/", derived: false, component: "web", open_path: "/"}],
             open: "http://127.0.0.1:61999/some/long/path"};
  const prev = {snap: snap, view: view};
  try {
    snap = {...snap, projects: [...snap.projects, p]};
    render();
    const line = document.querySelector("#p-w1-long-fixture .line");
    const cells = [...line.children].map(el => ({cls: el.className, r: el.getBoundingClientRect()}));
    let overlap = null;
    for (let i = 1; i < cells.length; i++)
      if (cells[i].r.left < cells[i-1].r.right - 0.5) overlap = cells[i-1].cls + ">" + cells[i].cls;
    return {overlap, cells: cells.map(c => c.cls)};
  } finally { snap = prev.snap; view = prev.view; render(); }
})()`;

async function main() {
  fs.mkdirSync(OUT, {recursive: true});
  const profile = fs.mkdtempSync("/tmp/w1-chrome-");
  const chrome = spawn(CHROME, [
    "--headless=new", "--disable-gpu", "--no-proxy-server",
    `--remote-debugging-port=${PORT}`, "--window-size=960,600",
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
    if (r.exceptionDetails) throw new Error("eval: " + JSON.stringify(r.exceptionDetails).slice(0, 400));
    return r.result?.value;
  };
  const shot = async name => {
    const r = await send("Page.captureScreenshot", {format: "png"});
    fs.writeFileSync(path.join(OUT, name), Buffer.from(r.data, "base64"));
    console.log("SHOT", name);
  };
  const setViewport = (w, h) => send("Emulation.setDeviceMetricsOverride",
    {width: w, height: h, deviceScaleFactor: 1, mobile: false});

  await send("Page.enable");
  await send("Runtime.enable");

  // ---------- 960×600：主窗口最小尺寸 ----------
  await setViewport(960, 600);
  await send("Page.navigate", {url: URL});
  await sleep(3500);

  check("960 下真实页面加载", await ev("document.querySelectorAll('.svc').length") > 0,
        await ev("document.querySelectorAll('.svc').length"));

  const projectLayout = await ev(`(() => {
    const project = projectGroups().find(g => !g.orphan && g.members.length > 1 && g.pj.directories?.length);
    if (!project) return {fixture: false};
    const sid = project.members[0].id;
    setView("project:" + project.pj.id);
    msgs[sid] = "有活动任务，已延后：2 个任务进行中";
    try {
      render();
      const services = document.querySelector(".project-services");
      const settings = document.querySelector(".project-config");
      const row = document.getElementById("p-" + sid);
      const message = row.querySelector(".service-message");
      return {fixture: true, separate: !!services?.contains(row) && !!settings?.querySelector("#projDirInput"),
              inset: message && message.getBoundingClientRect().left > row.getBoundingClientRect().left + 10,
              fits: document.getElementById("main").scrollWidth <= document.getElementById("main").clientWidth};
    } finally {
      delete msgs[sid];
      setView("all");
    }
  })()`);
  check("项目详情分别呈现服务与配置，操作提示缩进并适配窄窗",
        projectLayout.fixture && projectLayout.separate && projectLayout.inset && projectLayout.fits, JSON.stringify(projectLayout));

  const feedback = await ev(`(async () => {
    const service = snap.projects.find(p => p.state === "stopped");
    if (!service) return {fixture: false};
    const originalFetch = window.fetch;
    const sid = service.id;
    let response = {ok: true, accepted: true, message: "停止中"};
    window.fetch = (url, options) => String(url) === "/api/projects/" + sid + "/stop"
      ? Promise.resolve({status: response.ok ? 200 : 409, json: async () => response})
      : originalFetch(url, options);
    try {
      await act(sid, "stop");
      const message = () => document.querySelector("#p-" + sid + " .service-message");
      const transientCleared = !message();
      response = {ok: false, error: "有活动任务，暂不停止"};
      await act(sid, "stop");
      return {fixture: true, transientCleared, preserved: message()?.textContent === response.error};
    } finally {
      window.fetch = originalFetch;
      delete msgs[sid];
      render();
    }
  })()`, true);
  check("已受理操作不残留临时提示，阻塞原因保留在服务卡片",
        feedback.fixture && feedback.transientCleared && feedback.preserved, JSON.stringify(feedback));

  const overlapEv = `(() => {
    const bad = [];
    for (const line of document.querySelectorAll(".svc .line")) {
      const cells = [...line.children];
      for (let i = 1; i < cells.length; i++) {
        const prev = cells[i-1].getBoundingClientRect(), cur = cells[i].getBoundingClientRect();
        if (cur.left < prev.right - 0.5) bad.push(line.closest(".svc").id + ":" + (cells[i].className || cells[i].tagName));
      }
    }
    return {bad, docHscroll: document.documentElement.scrollWidth - document.documentElement.clientWidth,
            mainHscroll: document.getElementById("main").scrollWidth - document.getElementById("main").clientWidth};
  })()`;
  const o960 = await ev(overlapEv);
  check("960 服务行各列互不覆盖", o960.bad.length === 0, JSON.stringify(o960.bad.slice(0, 3)));
  check("960 无横向溢出（页面与内容区）", o960.docHscroll <= 0 && o960.mainHscroll <= 0, JSON.stringify(o960));

  const fx = await ev(FIXTURE_EV);
  check("960 长名/长地址夹具行互不覆盖", !fx.overlap, JSON.stringify(fx));

  // 设置视图：窄内容区单列可读（容器查询），长诊断不溢出
  const settingsEv = `(() => {
    setView("settings");
    const grid = document.querySelector(".settings-grid");
    const cols = getComputedStyle(grid).gridTemplateColumns.split(" ").filter(c => c && c !== "none").length;
    const cards = [...grid.querySelectorAll(".card")];
    const clipped = cards.filter(c => c.scrollWidth > c.clientWidth + 1).map(c => c.querySelector("h3")?.textContent);
    return {cols, clipped, gridWidth: grid.getBoundingClientRect().width,
            detailCard: cards.map(c => [c.querySelector("h3")?.textContent, Math.round(c.getBoundingClientRect().width)])};
  })()`;
  const st960 = await ev(settingsEv);
  check("960 设置视图单列（长诊断可读）", st960.cols === 1, JSON.stringify(st960.detailCard).slice(0, 120));
  check("960 设置卡片内容无横向裁切", st960.clipped.length === 0, JSON.stringify(st960.clipped));
  await shot("w1-settings-960.png");
  await ev("setView('all')");

  await shot("w1-workbench-960.png");

  // ---------- 顶部工具栏与侧栏 ----------
  const topEv = `(() => ({
    search: !!document.getElementById("globalSearch"),
    refresh: !!document.getElementById("refreshBtn"),
    settings: !!document.getElementById("nav-settings"),
    projectBadges: document.querySelectorAll("#navProjects .badge").length,
    primary: !!document.querySelector(".svc [data-act='open'], .svc [data-act='start-open']"),
  }))()`;
  const top = await ev(topEv);
  check("顶部搜索/刷新/设置可达", top.search && top.refresh && top.settings, JSON.stringify(top));
  check("侧栏项目项无常驻数字（无 badge）", top.projectBadges === 0, `badges=${top.projectBadges}`);
  check("服务行主操作可达（打开/启动并打开）", top.primary, "");

  // 搜索：过滤生效且可清空恢复
  const searchEv = `(() => {
    const input = document.getElementById("globalSearch");
    const total = document.querySelectorAll(".svc").length;
    input.value = "shicha";
    input.dispatchEvent(new Event("input"));
    const filtered = document.querySelectorAll(".svc").length;
    input.value = "";
    input.dispatchEvent(new Event("input"));
    return {total, filtered, restored: document.querySelectorAll(".svc").length};
  })()`;
  const sr = await ev(searchEv);
  check("搜索过滤并可清空恢复", sr.filtered < sr.total && sr.restored === sr.total, JSON.stringify(sr));

  // ---------- 轮询稳定性 ----------
  // 1) html 未变：连续 render 不重建（节点身份保持）
  const stableEv = `(() => {
    const btn = document.querySelector(".svc [data-act='logs']");
    btn.focus();
    render();
    return {same: document.activeElement === btn};
  })()`;
  check("状态未变时 render 保持节点身份（按钮不失焦）", await ev(stableEv).then(r => r.same), "");

  // 2) 状态变化：整树重建后焦点恢复到等价按钮（同服务行 + 同 data-act）
  const focusEv = `(() => {
    const btn = document.querySelector(".svc [data-act='logs'], .svc [data-act='open'], .svc [data-act='start-open']");
    const svcId = btn.closest(".svc").id, act = btn.dataset.act;
    btn.focus();
    const prev = {snap: snap};
    try {
      snap = JSON.parse(JSON.stringify(snap));
      snap.projects[0].label = "W1-POLL-CHANGED";
      render();
      const a = document.activeElement;
      return {restored: !!a && a.tagName === "BUTTON" && a.dataset && a.dataset.act === act
              && a.closest(".svc") && a.closest(".svc").id === svcId};
    } finally { snap = prev.snap; render(); }
  })()`;
  check("状态变化重建后焦点恢复到等价操作", await ev(focusEv).then(r => r.restored), "");

  const reordered = await ev(`(() => {
    const groups = registry.registry.projects;
    const service = groups.find(p => !p.favorite && p.services.length === 1 && document.querySelector("#p-" + p.services[0] + " [data-act='logs']"));
    if (!service) return {fixture: false};
    const selector = "#p-" + service.services[0] + " [data-act='logs']";
    const before = [...document.querySelectorAll(".svc")].map(row => row.id).join(",");
    document.querySelector(selector).focus();
    const saved = registry;
    try {
      registry = JSON.parse(JSON.stringify(registry));
      registry.registry.projects.find(p => p.id === service.id).favorite = true;
      render();
      return {fixture: true, moved: before !== [...document.querySelectorAll(".svc")].map(row => row.id).join(","),
              focused: document.activeElement === document.querySelector(selector),
              flat: !!document.querySelector(selector)?.closest(".group.single")};
    } finally { registry = saved; render(); }
  })()`);
  check("项目重排后焦点仍在同一服务和动作", reordered.fixture && reordered.moved && reordered.focused, JSON.stringify(reordered));
  check("单服务项目使用一张卡片", reordered.flat, JSON.stringify(reordered));

  // 3) 滚动锚点：上方内容高度变化后，视口内首个服务行保持原视觉位置
  const scrollEv = `(() => {
    const rows = [...document.querySelectorAll(".svc")];
    if (rows.length < 2) return {skip: true};
    const target = rows[Math.floor(rows.length / 2)];
    target.scrollIntoView({block: "start"});
    const topBefore = target.getBoundingClientRect().top;
    const prev = {snap: snap, open: {...open}};
    try {
      snap = JSON.parse(JSON.stringify(snap));
      snap.projects[0].description = "W1 让上方行副文本变长变高变长变高变长变高变长变高变长变高";
      render();
      const topAfter = document.getElementById(target.id).getBoundingClientRect().top;
      return {skip: false, dy: Math.abs(topAfter - topBefore)};
    } finally { snap = prev.snap; Object.keys(open).forEach(k => delete open[k]); Object.assign(open, prev.open); render(); }
  })()`;
  const sc = await ev(scrollEv);
  check("上方内容变化后滚动位置按锚点保持", sc.skip || sc.dy <= 2, JSON.stringify(sc));

  // 4) 展开态跨真实轮询周期保持（等 2 个 3s 周期，用页面自己的 refresh 路径）
  const expanded = await ev(`(() => {
    const row = document.querySelector(".svc .line");
    row.click();
    return !!document.querySelector(".svc.expanded .detail");
  })()`);
  check("点击行展开详情", expanded, "");
  await sleep(6500);   // 两个真实轮询周期（3s 间隔）
  const kept = await ev(`(() => {
    const d = document.querySelector(".svc.expanded .detail");
    return {detail: !!d, aria: document.querySelector(".svc .line")?.getAttribute("aria-expanded")};
  })()`);
  check("展开态跨真实轮询保持", kept.detail && kept.aria === "true", JSON.stringify(kept));

  // ---------- 常用宽窗（1280×900） ----------
  await setViewport(1280, 900);
  await sleep(400);
  const oWide = await ev(overlapEv);
  check("宽窗服务行各列互不覆盖", oWide.bad.length === 0, JSON.stringify(oWide.bad.slice(0, 3)));
  check("宽窗无横向溢出", oWide.docHscroll <= 0 && oWide.mainHscroll <= 0, JSON.stringify(oWide));
  const stWide = await ev(settingsEv);
  check("宽窗设置视图双栏", stWide.cols === 2, JSON.stringify(stWide.detailCard).slice(0, 120));
  await ev("setView('all')");
  await shot("w1-workbench-1280.png");
  const detailId = await ev("projectGroups().find(g => !g.orphan && g.members.length > 1 && g.pj.directories?.length)?.pj.id");
  if (detailId) {
    await ev(`setView(${JSON.stringify("project:" + detailId)}); window.scrollTo(0, 0)`);
    await shot("w1-project-detail-1280.png");
  }


  console.log(results.join("\n"));
  const fails = results.filter(l => l.startsWith("FAIL")).length;
  console.log(`\n==== ${results.length - fails} passed, ${fails} failed ====`);
  fs.writeFileSync(path.join(OUT, "w1-browser.txt"), results.join("\n") + "\n");
  cleanup();
  process.exit(fails ? 1 : 0);
}

main().catch(e => { console.error("FATAL:", e); process.exit(2); });
