#!/usr/bin/env node
/* W2 故障路径（真实 Chromium + 隔离 daemon，LOCOVE-WORKBENCH-UI）：
 *  A. 真实写路径：crash 循环夹具 → failed（查看原因 → 诊断行聚焦）；端口被外部进程
 *     占用 → conflict；正常夹具 → available。从服务行与「需要处理」两处进入诊断；
 *     ⋯ 菜单重试（请求形状 {open:true}，daemon 侧 open 会调系统浏览器，故拦截）；
 *     修复后真实重试（API，无 open）恢复 available；日志按钮加载日志；
 *     #p-<id> 深链载入即展开定位；lsDiagnose 与浏览器同一诊断路径。
 *  B. 九态形状（注入夹具，finally 还原真实 snap）：
 *     available/stopped（真实）/preparing/partial（注入）/failed/conflict（真实）/
 *     external/observe-only/daemon 断连（注入）。
 * 隔离：空闲端口 + 独立 LOCALSERVICES_HOME + 独立 launchd label 前缀；不碰 47800 与真实登记。
 * 用法：node scripts/verify_w2_browser.cjs [证据目录] */
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const REPO = path.resolve(__dirname, "..");
const OUT = process.argv[2] || "artifacts/acceptance-LOCOVE-WORKBENCH-UI";
const PORT = 9340;
const UID_ = require("os").userInfo().uid;

const sleep = ms => new Promise(r => setTimeout(r, ms));
const results = [];
const check = (name, cond, detail) => { results.push(`${cond ? "PASS" : "FAIL"} ${name}${cond ? "" : " :: " + (detail ?? "")}`); };
function freePort() {
  return new Promise((res, rej) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => { const p = srv.address().port; srv.close(() => res(p)); });
    srv.on("error", rej);
  });
}

async function main() {
  fs.mkdirSync(OUT, {recursive: true});
  // ---------- 隔离 daemon + 夹具 ----------
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ls-w2-"));
  const home = path.join(tmp, "home"), servicesDir = path.join(tmp, "services"), src = path.join(tmp, "src");
  for (const d of [home, servicesDir, src]) fs.mkdirSync(d, {recursive: true});
  fs.writeFileSync(path.join(src, "VERSION"), "w2\n");
  const crashFile = path.join(tmp, "crash");
  fs.writeFileSync(crashFile, "1\n");
  const dPort = await freePort(), okPort = await freePort(), badPort = await freePort(), foreignPort = await freePort();
  const base = `http://127.0.0.1:${dPort}`;
  const prefix = `localservices.w2verify.${Date.now()}`;
  const daemon = spawn("python3", ["-m", "localservices.daemon"], {
    cwd: REPO, env: {...process.env, LOCALSERVICES_HOME: home, LOCALSERVICES_SERVICES: servicesDir,
                          LOCALSERVICES_PORT: String(dPort), LOCALSERVICES_LABEL_PREFIX: prefix,
                          LOCOVE_ADMIN_PORT: String(await freePort()), LOCOVE_PROXY_PORT: "47899",
                          LOCOVE_FORCE_REAPPLY: "3600"},
    stdio: ["ignore", "ignore", "ignore"],
  });
  // 外部进程占住 foreignPort（非本工具启动）→ w2-conflict 启动后判冲突
  const foreign = spawn("python3", [path.join(REPO, "tests/fixture_server.py"), String(foreignPort)],
    {cwd: REPO, stdio: ["ignore", "ignore", "ignore"]});
  const cleanupAll = () => {
    try { daemon.kill("SIGTERM"); } catch (e) {}
    try { foreign.kill("SIGTERM"); } catch (e) {}
    try {
      for (const f of fs.readdirSync(path.join(home, "agents"))) {
        if (!f.endsWith(".plist")) continue;
        spawnSync("launchctl", ["bootout", `gui/${UID_}`, f.replace(/\.plist$/, "")], {stdio: "ignore"});
      }
    } catch (e) {}
    try { fs.rmSync(tmp, {recursive: true, force: true}); } catch (e) {}
  };
  process.on("exit", cleanupAll);

  let token = "";
  for (let i = 0; i < 100; i++) {
    try {
      const r = await fetch(`${base}/api/ping`);
      if (r.ok) { const t = fs.readFileSync(path.join(home, "token"), "utf8").trim(); if (t.length >= 32) { token = t; break; } }
    } catch (e) { /* not yet */ }
    await sleep(100);
  }
  if (!token) { console.error("FATAL: 隔离 daemon 未就绪"); process.exit(2); }
  const H = {"X-LocalServices-Token": token, "Content-Type": "application/json"};
  const api = async (method, p, body) => {
    const r = await fetch(base + p, {method, headers: H, body: body ? JSON.stringify(body) : undefined});
    let d = null; try { d = await r.json(); } catch (e) {}
    return [r.status, d];
  };

  const comp = (port, extra) => ({id: "web", kind: "server", name: "web",
    cmd: ["python3", path.join(REPO, "tests/fixture_server.py"), String(port), ...(extra || [])],
    cwd: ".", ...(port ? {port, ready: {http: `http://127.0.0.1:${port}/`}} : {})});
  const regBody = (sid, components) => ({schemaVersion: 1, revision: 0,
    project: {id: "w2fix", name: "W2 诊断夹具", agent: "none"},
    directories: [{path: src, role: "source"}], by: "verify",
    service: {id: sid, name: sid, description: "W2 夹具", source: src,
              open: `http://127.0.0.1:${okPort}/`, evidence: {agent: "none", summary: "verify"},
              components}});
  let rev = 0;
  const register = async (sid, components) => {
    const [code, res] = await api("POST", "/api/register", {...regBody(sid, components), revision: rev});
    if (code !== 200) throw new Error(`register ${sid}: ${code} ${JSON.stringify(res)}`);
    rev = res.revision ?? rev;
    return res;
  };
  await register("w2-ok", [comp(okPort)]);
  await register("w2-crash", [comp(badPort, ["--crash-file", crashFile])]);
  await register("w2-conflict", [comp(foreignPort)]);
  await register("w2-stopped", [comp(await freePort())]);

  const waitState = async (sid, states, timeout) => {
    const t0 = Date.now();
    for (;;) {
      const [, st] = await api("GET", "/api/status");
      const p = (st.projects || []).find(x => x.id === sid);
      if (p && states.includes(p.state)) return p;
      if (Date.now() - t0 > timeout) return p || null;
      await sleep(1000);
    }
  };
  await api("POST", "/api/projects/w2-ok/start", {});
  const okP = await waitState("w2-ok", ["available"], 40000);
  check("A: 正常夹具启动 available", okP && okP.state === "available", okP && okP.state);
  await api("POST", "/api/projects/w2-conflict/start", {});
  const cfP = await waitState("w2-conflict", ["conflict", "failed"], 40000);
  check("A: 端口被外部进程占用 → conflict", cfP && cfP.state === "conflict",
        cfP && `${cfP.state}:${(cfP.reason || "").slice(0, 60)}`);
  await api("POST", "/api/projects/w2-crash/start", {});
  const crP = await waitState("w2-crash", ["failed"], 150000);
  check("A: crash 循环夹具 → failed（gave up）", crP && crP.state === "failed",
        crP && `${crP.state}:${(crP.reason || "").slice(0, 60)}`);

  // ---------- 真实 Chromium ----------
  const profile = fs.mkdtempSync("/tmp/w2-chrome-");
  const chrome = spawn(CHROME, [
    "--headless=new", "--disable-gpu", "--no-proxy-server",
    `--remote-debugging-port=${PORT}`, "--window-size=1280,900",
    `--user-data-dir=${profile}`, "--no-first-run", "about:blank",
  ], {stdio: ["ignore", "ignore", "pipe"]});
  chrome.stderr.on("data", () => {});
  process.on("exit", () => { try { chrome.kill(); } catch (e) {} try { fs.rmSync(profile, {recursive: true, force: true}); } catch (e) {} });
  let target = null;
  for (let i = 0; i < 50 && !target; i++) {
    await sleep(200);
    try { const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); target = list.find(t => t.type === "page"); } catch (e) {}
  }
  if (!target) throw new Error("devtools page target not found");
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let seq = 0; const pending = new Map();
  ws.onmessage = ev => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
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
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Page.navigate", {url: `${base}/#p-w2-ok`});
  await sleep(3500);

  // 深链：#p-w2-ok 载入即展开定位
  const deep = await ev(`(() => {
    const row = document.getElementById("p-w2-ok");
    const line = row?.querySelector(".line");
    const r = row.getBoundingClientRect();
    return {row: !!row, expanded: line?.getAttribute("aria-expanded") === "true",
            detail: !!row.querySelector(".detail"), inViewport: r.top >= 0 && r.bottom <= innerHeight + 40};
  })()`);
  check("A: #p-<id> 深链载入即展开并定位", deep.row && deep.expanded && deep.detail && deep.inViewport, JSON.stringify(deep));

  // 服务行 → 查看原因 → 诊断行聚焦 + 内容（reason 是运行事实，断言非空 + 分层结构）
  const reasonEv = `(() => {
    const btn = document.querySelector("#p-w2-crash [data-act='reason']");
    if (!btn) return {btn: false};
    btn.click();
    const detail = document.querySelector("#p-w2-crash .detail");
    const reasonRow = document.getElementById("reason-w2-crash");
    const focused = detail.contains(document.activeElement);
    return {btn: true, expanded: !!detail, focused,
            reasonText: reasonRow ? (reasonRow.textContent || "").trim() : "",
            sections: ["概览", "日志"].every(h => detail.textContent.includes(h))};
  })()`;
  const rs = await ev(reasonEv);
  check("A: 服务行「查看原因」展开诊断并聚焦诊断行", rs.btn && rs.expanded && rs.focused, JSON.stringify(rs));
  check("A: 诊断展示原因（运行事实）与分层内容", rs.reasonText.length > 0 && rs.sections, JSON.stringify(rs).slice(0, 140));

  const local = await ev(`(() => {
    const row = document.getElementById("p-w2-crash");
    const ready = !!row.querySelector(".diagnosis [data-act='retry']")
      && !!row.querySelector(".diagnosis [data-act='copy-diagnosis']");
    row.querySelector("[data-act='reason']").click();
    const collapsed = !row.isConnected && !document.querySelector("#p-w2-crash .detail");
    const search = document.getElementById("globalSearch");
    search.value = "previous-unrelated-query";
    render();
    const hidden = !document.getElementById("p-w2-crash");
    window.lsDiagnose("w2-crash");
    return {ready, collapsed, hidden, focused: document.activeElement.id === "reason-w2-crash"};
  })()`);
  check("A: 诊断展示可执行动作和收起原因", local.ready && local.collapsed, JSON.stringify(local));
  check("A: 原生诊断跳转越过旧搜索并聚焦目标", local.hidden && local.focused, JSON.stringify(local));

  // 「需要处理」→ 同一服务诊断（reason 是运行事实：诊断行展示 snap 当前 reason）
  const attnEv = `(() => {
    const nav = [...document.querySelectorAll("nav .navitem")].find(b => b.textContent.includes("需要处理"));
    const badge = document.getElementById("attentionBadge");
    const n = badge && !badge.hidden ? Number(badge.textContent) : 0;
    nav.click();
    const rows = [...document.querySelectorAll(".svc")].map(s => s.id);
    const p = snap.projects.find(x => x.id === "w2-conflict");
    document.querySelector("#p-w2-conflict [data-act='reason']").click();
    const detail = document.querySelector("#p-w2-conflict .detail");
    const reasonRow = document.getElementById("reason-w2-conflict");
    return {n, rows, focused: detail.contains(document.activeElement),
            snapReason: (p && p.reason || "").slice(0, 60),
            reasonShown: !!(p && p.reason && reasonRow && reasonRow.querySelector("strong").textContent === p.reason.split("\\n", 1)[0])};
  })()`;
  const at = await ev(attnEv);
  check("A: 「需要处理」徽标数 = failed+conflict", at.n === 2, `badge=${at.n}`);
  check("A: 需要处理视图含两个故障行", at.rows.includes("p-w2-crash") && at.rows.includes("p-w2-conflict"), JSON.stringify(at.rows));
  check("A: 从「需要处理」进入同一服务诊断并聚焦", at.focused && at.reasonShown,
        `focused=${at.focused} snapReason=${at.snapReason}`);
  await ev("setView('all')");

  // ⋯ 菜单：failed 服务的重试（请求形状；daemon 侧 open 会调系统浏览器，故拦截 fetch）
  const retryEv = `(() => {
    const realFetch = window.fetch;
    let captured = null;
    window.fetch = async (p, opts) => {
      if (String(p).includes("/api/projects/w2-crash/start")) {
        captured = {path: String(p), body: opts && opts.body ? String(opts.body) : null};
        return {ok: true, status: 200, json: async () => ({ok: true, message: "已接受"})};
      }
      return realFetch(p, opts);
    };
    document.querySelector("#p-w2-crash .kebab").click();
    const items = [...document.querySelectorAll(".menu .menu-item")].map(b => b.textContent);
    const retry = [...document.querySelectorAll(".menu .menu-item")].find(b => b.textContent === "重试");
    if (retry) retry.click();
    return new Promise(res => setTimeout(() => { window.fetch = realFetch; res({items, captured}); }, 250));
  })()`;
  const rt = await ev(retryEv, true);
  check("A: failed 服务 ⋯ 菜单提供重试", rt.items.includes("重试"), JSON.stringify(rt.items));
  check("A: 重试发出 POST start {open:true}（形状）", !!rt.captured && rt.captured.body.includes('"open":true'),
        JSON.stringify(rt.captured));

  // 日志按钮 → 日志加载进详情
  const logsEv = `(() => {
    document.querySelector("#p-w2-crash [data-act='logs']").click();
    return new Promise(res => setTimeout(() => {
      const pre = document.getElementById("logs-w2-crash");
      res({pre: !!pre, text: pre ? pre.textContent.slice(0, 120) : null});
    }, 800));
  })()`;
  const lg = await ev(logsEv, true);
  check("A: 日志按钮加载日志到详情", lg.pre, JSON.stringify(lg).slice(0, 100));
  await ev(`(() => { open["w2-ok"] = false; window.lsDiagnose("w2-crash"); })()`);
  await shot("w2-diagnosis-failed.png");

  // 修复（移除 crash 文件）→ 真实重试（API，无 open）→ available → 行恢复「打开」
  fs.unlinkSync(crashFile);
  await api("POST", "/api/projects/w2-crash/start", {});
  const fixed = await waitState("w2-crash", ["available"], 40000);
  check("A: 修复后真实重试恢复 available", fixed && fixed.state === "available", fixed && fixed.state);
  const openBtn = await ev(`(async () => { await refresh(); const b = document.querySelector("#p-w2-crash [data-act='open']"); return b ? b.textContent : null; })()`, true);
  check("A: available 后行内主操作恢复「打开」", openBtn === "打开", openBtn);

  // lsDiagnose（原生「查看原因」调用的同一函数）
  const diag = await ev(`(() => {
    lsDiagnose("w2-crash");
    const d = document.querySelector("#p-w2-crash .detail");
    return {expanded: !!d, focused: d.contains(document.activeElement)};
  })()`);
  check("A: lsDiagnose（原生桥入口）与浏览器同路径", diag.expanded && diag.focused, JSON.stringify(diag));

  // ---------- B. 九态形状（注入，finally 还原） ----------
  const shapesEv = `(() => {
    const prev = {snap: snap, view: view};
    const mk = over => Object.assign({id: "w2-shape", name: "形状夹具", intent: "stopped", group: null,
      components: [], endpoints: [], description: "九态形状夹具"}, over);
    const out = {};
    const probe = (svc, menu) => {
      try {
        snap = {...snap, projects: [...snap.projects, svc]};
        render();
        const row = document.querySelector("#p-w2-shape");
        const primary = row.querySelector(".acts .primary");
        const stateTxt = row.querySelector(".statecell .state")?.textContent;
        let items = null;
        if (menu) {
          row.querySelector(".kebab").click();
          items = [...document.querySelectorAll(".menu .menu-item")].map(b => b.textContent);
          document.querySelector(".menu-backdrop")?.click();
        }
        return {stateTxt, primary: primary ? {text: primary.textContent, disabled: primary.disabled} : null, items};
      } finally { snap = prev.snap; render(); }
    };
    out.stopped = probe(mk({state: "stopped", label: "已停止"}));
    out.preparing = probe(mk({state: "preparing", label: "准备环境", op: null}));
    out.partial = probe(mk({state: "partial", label: "部分可用", can_open: true, reason: "依赖 postgres 不可用：连接被拒绝"}));
    out.external = probe(mk({state: "external", label: "外部实例", can_open: true, can_takeover: true, intent: "stopped"}), true);
    out.observe = probe(mk({observe_only: true, state: "available", label: "可用", detail: "引擎 3 实例在线",
                            actions: [{id: "refresh", label: "刷新数据"}]}), true);
    try {
      snap = {...snap, projects: [...snap.projects, mk({state: "failed", label: "故障", reason: "W2 注入：必要依赖未就绪",
        components: [{id: "api", name: "api", kind: "server", label: "", running: true, pid: 1, started_at: 1, code: {}}],
        dependencies: [{name: "postgres", ok: false, detail: "连接被拒绝", managed: false, note: "外部管理"}]})]};
      setView("attention");
      const btn = [...document.querySelectorAll("#p-w2-shape .acts button")].find(b => b.textContent === "查看原因");
      btn.click();
      const detail = document.querySelector("#p-w2-shape .detail");
      out.failedDiagnosis = {
        reasonFocused: detail.contains(document.activeElement),
        reason: detail.textContent.includes("W2 注入：必要依赖未就绪"),
        impact: detail.textContent.includes("依赖 postgres") && detail.textContent.includes("组件"),
      };
    } finally { snap = prev.snap; setView(prev.view); }
    try {
      setConn(false); render();
      out.disconnect = {
        banner: !document.getElementById("banner").hidden,
        unknownBtn: [...document.querySelectorAll(".svc .acts .primary")].filter(b => b.textContent.includes("状态未知")).length > 0
                    && [...document.querySelectorAll(".svc .acts .primary")].every(b => b.disabled),
      };
    } finally { setConn(true); render(); }
    return out;
  })()`;
  const sh = await ev(shapesEv);
  check("B: stopped → 启动并打开", sh.stopped.primary && sh.stopped.primary.text === "启动并打开" && !sh.stopped.primary.disabled, JSON.stringify(sh.stopped));
  check("B: preparing → 准备环境…（禁用）", sh.preparing.primary && sh.preparing.primary.text.includes("准备环境") && sh.preparing.primary.disabled, JSON.stringify(sh.preparing));
  check("B: partial → 可打开 + 状态可见", sh.partial.primary && sh.partial.primary.text === "打开" && sh.partial.stateTxt === "部分可用", JSON.stringify(sh.partial));
  check("B: external → 打开 + ⋯ 提供接管", sh.external.primary && sh.external.primary.text === "打开"
        && sh.external.items.includes("接管…") && !sh.external.items.includes("停止"), JSON.stringify(sh.external.items));
  check("B: observe-only → 无启动类主操作，状态可见，动作按 daemon 能力",
        sh.observe.primary === null && sh.observe.stateTxt === "可用"
        && sh.observe.items.includes("刷新数据") && sh.observe.items.includes("移除登记…")
        && !sh.observe.items.includes("停止") && !sh.observe.items.includes("重启"), JSON.stringify(sh.observe));
  check("B: failed 诊断展示影响（依赖/组件）与聚焦", sh.failedDiagnosis.reasonFocused && sh.failedDiagnosis.reason && sh.failedDiagnosis.impact, JSON.stringify(sh.failedDiagnosis));
  check("B: daemon 断连 → 横幅 + 全行状态未知（禁用）", sh.disconnect.banner && sh.disconnect.unknownBtn, JSON.stringify(sh.disconnect));
  const realRows = await ev(`(() => ({
    available: !!document.querySelector("#p-w2-ok [data-act='open']"),
    failedWas: true, conflictReason: ${JSON.stringify((cfP && cfP.reason || "").slice(0, 80))},
  }))()`);
  check("B: available（真实）行内主操作为打开", realRows.available, "");
  check("B: conflict（真实）原因来自运行事实（外部实例冲突）",
        /冲突|占用|非本工具启动|不会停止它/.test(realRows.conflictReason), realRows.conflictReason);

  await shot("w2-nine-states-after.png");

  console.log(results.join("\n"));
  const fails = results.filter(l => l.startsWith("FAIL")).length;
  console.log(`\n==== ${results.length - fails} passed, ${fails} failed ====`);
  fs.writeFileSync(path.join(OUT, "w2-browser.txt"), results.join("\n") + "\n");
  // 清理：停服务（bootout 隔离 label）→ 杀 daemon/外部进程/Chrome → 删临时目录
  await api("POST", "/api/projects/w2-ok/stop", {}).catch(() => {});
  await api("POST", "/api/projects/w2-crash/stop", {}).catch(() => {});
  cleanupAll();
  try { chrome.kill(); } catch (e) {}
  process.exit(fails ? 1 : 0);
}

main().catch(e => { console.error("FATAL:", e); process.exit(2); });
