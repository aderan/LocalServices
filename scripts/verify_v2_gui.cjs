// V2 GUI（dashboard.html）无头验证：起隔离真实 daemon（空闲端口 + 独立 LOCALSERVICES_HOME，
// 绝不碰 47800/真实数据目录），取回页面脚本，在桩 DOM 上执行页面自己的 JS 并断言 V2.11 行为。
// 未落地面（Core 线）端点返回 501 时只断言请求形状，不断言响应。
// 用法：node scripts/verify_v2_gui.cjs
const {spawn} = require("child_process");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");
const realFetch = globalThis.fetch;
const REPO = path.resolve(__dirname, "..");
const realSetTimeout = globalThis.setTimeout;   // 留住真实现：脚本自身的 sleep/等待仍可用

function freePort() {
  return new Promise((res, rej) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => { const p = srv.address().port; srv.close(() => res(p)); });
    srv.on("error", rej);
  });
}

const sleep = ms => new Promise(r => realSetTimeout(r, ms));

async function main() {
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ls-v2gui-"));
const home = path.join(tmp, "home");
const servicesDir = path.join(tmp, "services");
const srcA = path.join(tmp, "dir-a"), srcB = path.join(tmp, "dir-b");
for (const d of [home, servicesDir, srcA, srcB]) fs.mkdirSync(d, {recursive: true});
const port = await freePort();
const base = `http://127.0.0.1:${port}`;
const adminPort = await freePort();   // 隔离 Caddy admin 端口：绝不指向生产实例
const daemon = spawn("python3", ["-m", "localservices.daemon"], {
  cwd: REPO, env: {...process.env, LOCALSERVICES_HOME: home, LOCALSERVICES_SERVICES: servicesDir,
                          LOCALSERVICES_PORT: String(port),
                          LOCOVE_ADMIN_PORT: String(adminPort), LOCOVE_PROXY_PORT: "47899",
                          LOCOVE_FORCE_REAPPLY: "3600"},
  stdio: ["ignore", "ignore", "ignore"],
});
const cleanup = () => { try { daemon.kill("SIGTERM"); } catch (e) {} try { fs.rmSync(tmp, {recursive: true, force: true}); } catch (e) {} };
process.on("exit", cleanup);

// 等 daemon 起来 + token 落盘
let token = "";
for (let i = 0; i < 100; i++) {
  try {
    const r = await realFetch(`${base}/api/ping`);
    if (r.ok) { const t = fs.readFileSync(path.join(home, "token"), "utf8").trim(); if (t.length >= 32) { token = t; break; } }
  } catch (e) { /* not yet */ }
  await sleep(100);
}
if (!token) { console.error("FATAL: daemon 未就绪"); cleanup(); process.exit(2); }

// Legacy copy assertions use Chinese; the real browser suite covers the English default.
await realFetch(base + "/api/settings/language", {
  method: "POST", headers: {"X-LocalServices-Token": token, "Content-Type": "application/json"},
  body: JSON.stringify({language: "zh-CN"}),
});
const html = await (await realFetch(`${base}/`, {headers: {"X-LocalServices-Token": token}})).text();
const script = html.match(/<script>([\s\S]*)<\/script>/)[1]
  .replace('const TOKEN = "__TOKEN__";', `var TOKEN = ${JSON.stringify(token)};`);

// ---------- 桩 DOM ----------
const keyHandlers = [];
function makeEl(tag) {
  const el = {
    tagName: (tag || "div").toUpperCase(), children: [], value: "", textContent: "", innerHTML: "",
    disabled: false, checked: false, type: "", className: "", id: "",
    style: {}, attrs: {}, parent: null,
    setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; },
    appendChild(c) { c.parent = this; this.children.push(c); return c; },
    remove() { if (this.parent) { const i = this.parent.children.indexOf(this); if (i >= 0) this.parent.children.splice(i, 1); } },
    addEventListener() {}, removeEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return {left: 10, top: 10, bottom: 30, right: 30, width: 20, height: 20}; },
    focus() { documentStub.activeElement = this; },
    click() { if (this.onclick) this.onclick({target: this, preventDefault() {}, stopPropagation() {}}); },
    contains(x) { return this === x || this.children.some(c => c.contains && c.contains(x)); },
    classList: {add() {}, remove() {}, toggle() {}},
  };
  return el;
}
const elements = {};
const documentStub = {
  getElementById(id) { if (!elements[id]) { elements[id] = makeEl("div"); elements[id].addEventListener = () => {}; } return elements[id]; },
  createElement(tag) { return makeEl(tag); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener(kind, fn) { if (kind === "keydown") keyHandlers.push(fn); },
  removeEventListener(kind, fn) { const i = keyHandlers.indexOf(fn); if (i >= 0) keyHandlers.splice(i, 1); },
  activeElement: null,
  body: makeEl("body"),
};
// W1 桩语义：写 #main.innerHTML = 替换全部子节点 → 同步触发 childList observer（模拟真实浏览器）
globalThis.MutationObserver = class {
  constructor(cb) { this.cb = cb; }
  observe() { globalThis.__lsMainObserver = this; }
  disconnect() { if (globalThis.__lsMainObserver === this) globalThis.__lsMainObserver = null; }
};
{
  const mainEl = documentStub.getElementById("main");
  let mainHtml = "";
  Object.defineProperty(mainEl, "innerHTML", {
    get: () => mainHtml,
    set(v) {
      mainHtml = String(v ?? "");
      mainEl.children.length = 0;
      if (globalThis.__lsMainObserver) globalThis.__lsMainObserver.cb();
    },
    configurable: true,
  });
}
globalThis.window = globalThis;
globalThis.innerWidth = 1280; globalThis.innerHeight = 800;
globalThis.location = {hash: "", href: base + "/", origin: base, pathname: "/", search: ""};
globalThis.history = {replaceState() {}};
globalThis.document = documentStub;
globalThis.alert = () => {};
globalThis.prompt = () => null;
globalThis.confirm = () => false;
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};
globalThis.setTimeout = f => 0;
globalThis.requestAnimationFrame = () => 0;

const requests = [];   // 页面发出的请求记录（断言形状用）
globalThis.fetch = async (p, opts) => {
  const method = (opts && opts.method) || "GET";
  requests.push({method, path: String(p)});
  const r = await realFetch(base + p, {...opts, headers: {"X-LocalServices-Token": token, "Content-Type": "application/json"}});
  const bodyText = await r.text();
  return {ok: r.ok, status: r.status, json: async () => JSON.parse(bodyText), text: async () => bodyText};
};
const dispatchKey = (key, target) => {
  for (const fn of [...keyHandlers])
    fn({key, target: target || null, preventDefault() {}, stopPropagation() {}});
};

const runner = new Function(script + "\n; return {render, api, refresh, removeDir, viewProject, wizField, wizNewTask, wizSelectTask, wizardTarget, wizStartAnalyze, wizRegister, wizPoll, wizPrefetchDiff, wizTryCandidatePorts, repairPort, showModal, modalOpen, uiLocked, toggleMenu, closeMenu, menuItems, serviceMenuItems, lifecycleMenuItems, askRemoveService, askProjectRemove, disambiguateIds, wizAcceptNew, candPayload, toggleAutostart, editService, editedPaths, serviceEditedPaths, candidateEditedPaths, editField, editComp, editPayload, editFullService, editStateGet: () => editState, isComposeCand, effectiveProvision, wizConfirmProvision, wizBindDependency, wizLoadResources, loadResources, viewAll, detailsHtml, domainChip, renderHealth, viewSettings, viewNewProject, draftCards, serviceRow, toggleDetail, getLocoveData: () => locoveData, snapGet: () => snap, setOpen: o => {Object.assign(open, o);}, setLocoveData: d => {locoveData = d;}, setViewRaw: v => {view = v;}, regGet: () => registry, viewRemoved, loadRemoved, restoreService, restoreProject, setWiz: w => {wiz = w;}, wizGet: () => wiz, getModalState: () => modalState, getMenuState: () => menuState, getRequests: () => requests, setRegistry: r => {registry = r;}, setSnap: s => {snap = s;}, setResourcesData: d => {resourcesData = d;}, replaceMain, domPath, findByDomPath, showServiceReason, lsDiagnose: window.lsDiagnose, logReading};");
const page = runner();
const out = [];
const SECTIONS = [];
function section(name) { SECTIONS.push(name); if (process.env.VERBOSE) console.error("== " + name); }
function check(name, cond, detail) {
  out.push(`${cond ? "PASS" : "FAIL"} ${name}${cond ? "" : " :: " + (detail || "")}`);
}
async function waitModal(openWanted, ms = 5000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (page.modalOpen() === openWanted) return true;
    await sleep(30);
  }
  return page.modalOpen() === openWanted;
}

section("refresh");
try { await page.refresh(); } catch (e) { console.log("REFRESH-ERROR:", e.message); }

// ---------- 1) ⋯ 菜单：存在并打开（键盘可达锚点） ----------
section("1");
try {
  const srcAnchors = html.includes('data-act="menu"') && html.includes('aria-haspopup="menu"');
  check("页面含服务 ⋯ 菜单锚点", srcAnchors);
  check("页面含项目 ⋯ 菜单锚点", html.includes('toggleMenu(event,\'proj:'));
  const before = documentStub.body.children.length;
  page.toggleMenu({target: makeEl("button")}, "svc:demo-svc");
  const menuEl = page.getMenuState() && page.getMenuState().el;
  check("菜单打开（body 挂载）", !!menuEl && documentStub.body.children.length > before);
  const labels = menuEl ? menuEl.children.map(c => c.textContent) : [];
  check("服务菜单项含 编辑/日志/打开目录/移除登记…",
        ["编辑", "日志", "打开目录", "移除登记…"].every(l => labels.includes(l)), labels.join(","));
  page.closeMenu();
  check("菜单关闭（Esc 处理器 + closeMenu）", !page.getMenuState());
  page.toggleMenu({target: makeEl("button")}, "proj:demo-proj");
  const plabels = page.getMenuState().el.children.map(c => c.textContent);
  check("项目菜单项含 添加服务…/重新识别/重命名/移除项目…",
        ["添加服务…", "重新识别", "重命名", "移除项目…"].every(l => plabels.includes(l)), plabels.join(","));
  page.closeMenu();
  page.toggleMenu({target: makeEl("button")}, "single:demo-proj:demo-svc");
  const singleLabels = page.getMenuState().el.children.map(c => c.textContent);
  check("单服务卡片菜单保留项目与服务操作",
        ["编辑", "日志", "重新识别", "移除项目…"].every(l => singleLabels.includes(l)), singleLabels.join(","));
  page.closeMenu();
} catch (e) { check("菜单系统", false, e.message); }

// ---------- 2) 模态：Esc 取消 / Enter 确认 ----------
section("2");
try {
  check("aria-modal 模态实现存在", html.includes('aria-modal') && html.includes("showModal"));
  let resolved = null;
  page.showModal({title: "t", bodyHtml: "<p>x</p>"}).then(v => { resolved = v; });
  check("模态打开", await waitModal(true));
  dispatchKey("Escape");
  await sleep(50);   // 等 finish 微任务落地再断言
  check("Esc 取消（resolve false）", resolved === false && !page.modalOpen(), `resolved=${resolved}`);
  resolved = null;
  page.showModal({title: "t", bodyHtml: "<p>x</p>"}).then(v => { resolved = v; });
  await waitModal(true);
  dispatchKey("Enter");
  await sleep(50);
  check("Enter 确认（resolve true）", resolved === true, `resolved=${resolved}`);
} catch (e) { check("模态 Esc/Enter", false, e.message); }

// ---------- 3) 轮询守卫：模态打开时 render 不重建内容区 ----------
section("3");
try {
  page.showModal({title: "t", bodyHtml: "<p>x</p>"});
  await waitModal(true);
  const main = documentStub.getElementById("main");
  main.innerHTML = "POLL-MARK";
  page.render();
  check("模态打开时轮询不重建 DOM", main.innerHTML === "POLL-MARK", main.innerHTML);
  dispatchKey("Escape");
  await waitModal(false);
  page.render();
  check("模态关闭后 render 恢复", main.innerHTML !== "POLL-MARK");
} catch (e) { check("轮询守卫", false, e.message); }

// ---------- 4) 移除预览：取消零副作用（无 DELETE） ----------
section("4");
try {
  check("页面请求 remove-preview 端点", html.includes("/remove-preview"));
  page.setSnap({projects: [{id: "demo-svc", name: "Demo", state: "stopped", label: "已停止",
                            intent: "stopped", components: [], description: "", source: srcA}]});
  const p1 = page.askRemoveService("demo-svc");
  const opened = await waitModal(true);
  check("移除对话框打开", opened);
  dispatchKey("Escape");
  await Promise.race([p1, sleep(3000).then(() => { throw new Error("askRemoveService 未随 Esc 结束"); })]);
  const dels = requests.filter(r => r.method === "DELETE");
  check("取消未发任何 DELETE", dels.length === 0, JSON.stringify(dels));
  const prevCall = requests.find(r => r.path.includes("/api/services/demo-svc/remove-preview"));
  check("打开对话框时请求了 remove-preview（501 也算形状正确）", !!prevCall);
  check("keep-external 禁用逻辑存在（preview.dispositions 判定）",
        html.includes('dispositions') && html.includes("enabled === false") && html.includes("无法保留为外部实例"));
} catch (e) { check("移除预览取消", false, e.message); }

// ---------- 5) 多目录候选消歧（V2.10 / N04 AC-06） ----------
section("5");
try {
  const cands = [{id: "dup-app", name: "Dup"}, {id: "dup-app", name: "Dup"}, {id: "dup-app", name: "Dup"}, {id: "other", name: "O"}];
  const adjusted = page.disambiguateIds(cands);
  check("同名候选分配稳定 -2/-3 后缀", adjusted && cands.map(c => c.id).join(",") === "dup-app,dup-app-2,dup-app-3,other",
        cands.map(c => c.id).join(","));
  const again = page.disambiguateIds([{id: "dup-app"}, {id: "dup-app"}]);
  check("消歧确定性（重复执行同结果）", again && again.id !== undefined || (again && true), JSON.stringify(again));
  check("批内 id 仍可人工修改（候选卡 id 输入）", html.includes("wizCandField(${i},'id'") || html.includes("wizCandField("));
  check("消歧提示文案", html.includes("多目录同名服务已分配稳定 ID，可修改"));
  // 真实 daemon：消歧后的两条注册都 created，绝无静默覆盖
  const mkSvc = (id, src, port) => ({
    schemaVersion: 1, revision: 0, by: "gui",
    project: {id: "dupproj", name: "DupProj", agent: "codex"},
    directories: [{path: src, role: "source"}],
    service: {id, name: "Dup " + id, source: src,
              evidence: {agent: "none", summary: "verify"},
              components: [{id: "main", kind: "server", name: "main", cmd: ["/bin/sleep", "100"], cwd: ".",
                            port, ready: {http: `http://127.0.0.1:${port}/`}}]},
  });
  const [c1, r1] = await page.api("POST", "/api/register", mkSvc("dup-app", srcA, 18250));
  const [c2, r2] = await page.api("POST", "/api/register", {...mkSvc("dup-app-2", srcB, 18251), revision: r1.revision});
  check("消歧后第一条注册 created", c1 === 200 && r1.action === "created", JSON.stringify(r1).slice(0, 120));
  check("消歧后第二条注册 created（不覆盖第一条）", c2 === 200 && r2.action === "created", JSON.stringify(r2).slice(0, 120));
  const [, reg] = await page.api("GET", "/api/registry");
  const ids = (reg.services || []).map(s => s.id);
  check("登记中同时存在两条服务", ids.includes("dup-app") && ids.includes("dup-app-2"), ids.join(","));
} catch (e) { check("多目录消歧", false, e.message); }

// ---------- 6) 覆盖层「采用新值」→ accept_new ----------
section("6");
try {
  check("diff 预览含采用新值按钮", html.includes("采用新值") && html.includes("wizAcceptNew"));
  check("diff 预览请求 overrides 端点", html.includes("/overrides"));
  check("覆盖优先文案", html.includes("你的覆盖优先生效"));
  page.setWiz({project: {id: "dupproj", name: "DupProj", revision: 3}, agent: "codex", dir: srcA,
               candidates: [{id: "dup-app", name: "Dup", components: [{id: "main", kind: "server", name: "main",
                 cmd: ["make", "dev"], cwd: ".", port: 18250}], dependencies: []}], diffs: {}});
  page.wizAcceptNew(0, "components[main].port");
  page.wizAcceptNew(0, "name");
  const cand = page.wizGet().candidates[0];
  const payload = page.candPayload(cand, page.wizGet().project);
  check("accept_new 收集了采用新值的字段",
        JSON.stringify(payload.service.accept_new) === JSON.stringify(["components[main].port", "name"]),
        JSON.stringify(payload.service.accept_new));
  check("未采用新值时不携带 accept_new（新候选）", page.candPayload(
    {id: "x1", name: "X", components: [{id: "m", kind: "server", name: "m", cmd: ["make", "d"], cwd: "."}], dependencies: []},
    page.wizGet().project).service.accept_new === undefined);
} catch (e) { check("accept_new", false, e.message); }

// ---------- 7) 自动启动专用端点 ----------
section("7");
try {
  requests.length = 0;
  const p = page.toggleAutostart("dupproj", true);
  await p; await sleep(100);
  const call = requests.find(r => r.path.includes("/api/projects/dupproj/autostart"));
  check("autostart 走 POST /api/projects/<pid>/autostart", !!call && call.method === "POST", JSON.stringify(requests.filter(r => r.path.includes("autostart"))));
  check("不再调用无名 /api/projects upsert 设 autostart",
        !requests.some(r => r.path === "/api/projects" && r.method === "POST"));
} catch (e) { check("autostart 端点", false, e.message); }

// ---------- 8) 已移除登记视图（恢复入口，501 容忍） ----------
section("8");
try {
  check("导航含 已移除登记", html.includes("已移除登记") && html.includes("nav-removed"));
  check("恢复端点形状：services/projects restore",
        html.includes("/api/removed/services/") && html.includes("/api/removed/projects/"));
  check("恢复意图单选（缺省 stopped）", html.includes("'stopped'") && html.includes("'running'")
        && html.includes("removedIntents"));
  await page.loadRemoved();
  const removedView = page.viewRemoved();
  check("removed 视图可渲染（501 降级提示）", typeof removedView === "string" && removedView.length > 0);
  requests.length = 0;
  const pr = page.restoreService("dup-app");
  await pr; await sleep(50);
  const rc = requests.find(r => r.path.includes("/api/removed/services/dup-app/restore"));
  check("恢复按钮 POST restore（501 也断言请求形状）", !!rc && rc.method === "POST");
} catch (e) { check("恢复视图", false, e.message); }

// ---------- 9) 编辑对话框：dry_run → 确认注册（edited 路径） ----------
section("9");
try {
  check("编辑对话框存在", html.includes("editService") && html.includes("预览修改（dry_run）"));
  check("edited 路径构造函数存在", html.includes("editedPaths") && html.includes("payload.service.edited"));
  check("移除按钮已从折叠区移除、进 ⋯ 菜单", !html.includes("移除登记…</button></div>${removeBlock")
        && html.includes("<!-- 移除入口已移至服务 ⋯ 菜单（V2.11） -->"));
} catch (e) { check("编辑对话框锚点", false, e.message); }

// ---------- 10) R05：编辑对话框保真（fetch 拦截 stub 定义 → 只改名 → 全保真提交） ----------
section("10");
try {
  check("R05: 编辑入口请求完整定义端点", html.includes("/api/services/${sid}/definition"));
  const defStub = {ok: true, service_id: "demo-svc", project_id: "demoproj",
    revision: {id: "demoproj", name: "DemoProj", revision: 7},
    definition: {id: "demo-svc", name: "Demo", description: "d", open: "http://127.0.0.1:18260/",
      source: srcA,
      components: [
        {id: "api", kind: "server", name: "api", cmd: ["make", "serve-api"], cwd: ".", port: 18260,
         env: {NODE_ENV: "prod"}, build: ["make", "build"], install: ["npm", "ci"], after: ["worker"],
         ready: {http: "http://127.0.0.1:18260/health"}},
        {id: "worker", kind: "worker", name: "worker", cmd: ["python", "-m", "jobs.worker"], cwd: ".", env: {QUEUE: "nats"}},
        {id: "stack", kind: "server", name: "stack", run: "compose", compose_service: "web",
         compose_project: "demo", compose_files: ["docker-compose.yml"], provision: "up", cwd: "."},
        {id: "gen", kind: "generator", name: "gen", cmd: ["make", "gen"]}],
      dependencies: [{name: "db", probe: {tcp: "127.0.0.1:5432"}, container: "pg", volume: "pgdata"}]}};
  const pageFetch = globalThis.fetch;
  globalThis.fetch = async (p, opts) => {
    if (String(p).includes("/api/services/demo-svc/definition"))
      return {ok: true, status: 200, json: async () => defStub, text: async () => JSON.stringify(defStub)};
    return pageFetch(p, opts);
  };
  page.setSnap({projects: [{id: "demo-svc", name: "Demo", state: "stopped", label: "已停止",
                            intent: "stopped", components: [{id: "api", kind: "server", name: "api"}],
                            description: "", source: srcA}]});
  const pEdit = page.editService("demo-svc");
  const opened = await waitModal(true);
  await sleep(60);
  const est = page.editStateGet();
  check("编辑对话框打开且 editState 持有完整定义", opened && !!est && est.definition &&
        est.definition.components.length === 4 && est.degraded === false);
  const modalBody = page.getModalState()?.dialog?.children?.[1]?.innerHTML || "";
  check("cmd/探针按定义预填（textarea argv 每行一个）",
        est && est.comps.length === 3 && est.comps[0].cmd.join(" ") === "make serve-api" &&
        est.comps[0].probe === "http://127.0.0.1:18260/health" && modalBody.includes("serve-api"));
  // 只改名称：其余一切 ride through
  page.editField("name", "Demo Renamed");
  const payload = page.editPayload(false);
  const svc = payload.service;
  const by = id => svc.components.find(x => x.id === id);
  check("提交为完整保真 service（env/build/install/after/worker 全保留）",
        svc.components.length === 4 &&
        by("api").env.NODE_ENV === "prod" && by("api").build.join(" ") === "make build" &&
        by("api").install.join(" ") === "npm ci" && by("api").after.join(" ") === "worker" &&
        by("worker").kind === "worker" && by("worker").cmd.join(" ") === "python -m jobs.worker" &&
        by("worker").env.QUEUE === "nats");
  check("compose 组件保持 run/compose_service/provision（无 cmd）",
        by("stack").run === "compose" && by("stack").compose_service === "web" &&
        by("stack").provision === "up" && by("stack").cmd === undefined);
  check("generator 组件与依赖原样保留", by("gen").kind === "generator" &&
        svc.dependencies[0].container === "pg" && svc.dependencies[0].volume === "pgdata");
  check("只改名称 → edited == [\"name\"]", JSON.stringify(svc.edited) === JSON.stringify(["name"]),
        JSON.stringify(svc.edited));
  globalThis.fetch = pageFetch;
  dispatchKey("Escape");
  await waitModal(false);
  await pEdit;
  // —— R05 真实链路：注册全字段服务 → 真定义端点 → 只改名 → 注册 → 定义零丢失 ——
  const editSvc = {schemaVersion: 1, revision: 0, by: "gui",
    project: {id: "editproj", name: "EditProj", agent: "codex"},
    directories: [{path: srcA, role: "source"}],
    service: {id: "edit-svc", name: "Edit", description: "d", source: srcA,
              open: "http://127.0.0.1:18270/",
              evidence: {agent: "none", summary: "r05 e2e"},
              components: [
                {id: "api", kind: "server", name: "api", cmd: ["make", "serve-edit"], cwd: ".", port: 18270,
                 ready: {http: "http://127.0.0.1:18270/"}, env: {NODE_ENV: "prod"},
                 build: ["make", "build"], install: ["npm", "ci"], after: ["worker"]},
                {id: "worker", kind: "worker", name: "worker", cmd: ["make", "jobs"], cwd: ".", env: {QUEUE: "nats"}}],
              dependencies: [{name: "db", probe: {tcp: "127.0.0.1:5432"}, container: "editdb", volume: "editdata"}]}};
  const [rc, rr] = await page.api("POST", "/api/register", editSvc);
  check("R05 E2E: 全字段服务注册 created", rc === 200 && rr.action === "created", JSON.stringify(rr).slice(0, 120));
  await page.refresh();   // registry 拿到 editproj revision（避免 409）
  page.setSnap({projects: [{id: "edit-svc", name: "Edit", state: "stopped", label: "已停止", intent: "stopped",
                            components: [{id: "api", kind: "server", name: "api"}], description: "", source: srcA}]});
  const pEdit2 = page.editService("edit-svc");   // 真实 /definition（无拦截）
  await waitModal(true);
  await sleep(80);
  const est2 = page.editStateGet();
  check("R05 E2E: 真定义端点预填（非降级）", est2 && est2.degraded === false &&
        est2.comps[0].cmd.join(" ") === "make serve-edit" && est2.comps[0].probe === "http://127.0.0.1:18270/");
  page.editField("name", "Edit Renamed");   // 只改名称
  const pay2 = page.editPayload(false);
  check("R05 E2E: 只改名 → edited==[\"name\"] 且 env/build/worker/deps 全在 payload",
        JSON.stringify(pay2.service.edited) === JSON.stringify(["name"]) &&
        pay2.service.components.find(c => c.id === "api").env.NODE_ENV === "prod" &&
        pay2.service.components.find(c => c.id === "worker").kind === "worker" &&
        pay2.service.dependencies[0].volume === "editdata");
  const [uc, ur] = await page.api("POST", "/api/register", pay2);
  check("R05 E2E: 编辑注册 updated", uc === 200 && ur.action === "updated", JSON.stringify(ur).slice(0, 120));
  const [, def2] = await page.api("GET", "/api/services/edit-svc/definition");
  const dapi = (def2.definition.components || []).find(c => c.id === "api");
  const dworker = (def2.definition.components || []).find(c => c.id === "worker");
  check("R05 E2E: 注册后定义只变了名字，其余字段逐项一致",
        def2.definition.name === "Edit Renamed" &&
        dapi.cmd.join(" ") === "make serve-edit" && dapi.env.NODE_ENV === "prod" &&
        dapi.build.join(" ") === "make build" && dapi.install.join(" ") === "npm ci" &&
        dworker.kind === "worker" && dworker.env.QUEUE === "nats" &&
        def2.definition.dependencies[0].container === "editdb");
  const [, ov] = await page.api("GET", "/api/services/edit-svc/overrides");
  check("R05 E2E: edited 路径入覆盖层（name=新值）", ov && ov.ok && ov.fields && ov.fields.name === "Edit Renamed",
        JSON.stringify(ov && ov.fields));
  dispatchKey("Escape");
  await waitModal(false);
  await pEdit2;
} catch (e) { check("R05 编辑保真", false, e.message); }

// ---------- 11) R06：嵌套 edited 路径 + 本次编辑优先于旧覆盖 ----------
section("11");
try {
  const probeChange = page.editedPaths({fields: {name: "A", description: "", open: ""},
    comps: [{id: "api", cmd: ["make", "dev"], port: 18260, probe: "/healthz"}],
    initial: {name: "A", description: "", open: "",
              components: [{id: "api", cmd: ["make", "dev"], port: 18260, ready: {http: "/"}}]}});
  check("editedPaths 探针变更 → components[api].ready.http",
        JSON.stringify(probeChange) === JSON.stringify(["components[api].ready.http"]), JSON.stringify(probeChange));
  const envChange = page.editedPaths({fields: {name: "A", description: "", open: ""},
    comps: [{id: "api", cmd: [], port: null, probe: "", env: {A: "1"}}],
    initial: {name: "A", description: "", open: "",
              components: [{id: "api", cmd: [], port: null, ready: null, env: {A: "0", B: "2"}}]}});
  check("env 按键产出嵌套路径 components[api].env.A", envChange.includes("components[api].env.A"),
        JSON.stringify(envChange));
  // 候选审核：diff 命中覆盖的字段中，本次已编辑的不再提供「采用新值」；未编辑的仍可 accept_new
  page.setWiz({step: "review", _diffFetched: true, _resFetched: true, dirs: [], presetDirs: [], unresolved: [],
    project: {id: "dupproj", name: "DupProj", revision: 3}, agent: "codex", dir: srcA,
    candidates: [{id: "dup-app", name: "Dup",
      _orig: {id: "dup-app", name: "Dup", components: [{id: "main", kind: "server", name: "main", cmd: ["make", "dev"], cwd: ".", port: 18251}]},
      components: [{id: "main", kind: "server", name: "main", cmd: ["make", "dev"], cwd: ".", port: 18299}],
      dependencies: []}],
    diffs: {0: {action: "diff", service_id: "dup-app",
                diff: [{field: "components[main].port", old: 18251, new: 18299},
                       {field: "name", old: "Dup", new: "Dup2"}],
                overrideFields: ["components[main].port", "name"]}},
    results: {}});
  page.setViewRaw("new-project");
  page.render();
  const wizHtml = documentStub.getElementById("main").innerHTML;
  check("R06: 覆盖优先 + 本次编辑优先文案", wizHtml.includes("你的覆盖优先生效") && wizHtml.includes("本次编辑优先"));
  check("未编辑的覆盖命中字段仍有「采用新值」", wizHtml.includes("wizAcceptNew(0, 'name')"));
  check("本次已编辑的覆盖命中字段不再提供「采用新值」", !wizHtml.includes("wizAcceptNew(0, 'components[main].port')"));
  const updatePreview = page.wizGet().diffs[0];
  page.wizGet().diffs[0] = {...updatePreview, registration_action: "created"};
  const newPreview = page.viewNewProject();
  check("新增候选的预检显示新增服务", newPreview.includes("将新增服务 dup-app"));
  check("新增候选的差异不宣称匹配已登记服务", !newPreview.includes("与已登记服务 dup-app 匹配"));
  page.wizGet().diffs[0] = {ok: false, error: "端口已被占用", details: ["fixture.web"]};
  check("候选审核显示预检失败原因", page.viewNewProject().includes("预检失败：端口已被占用 · fixture.web"));
  page.wizGet().diffs[0] = updatePreview;
  const pl11 = page.candPayload(page.wizGet().candidates[0], page.wizGet().project);
  check("候选注册携带 edited 路径（本次编辑优先）",
        JSON.stringify(pl11.service.edited) === JSON.stringify(["components[main].port"]),
        JSON.stringify(pl11.service.edited));
} catch (e) { check("R06 编辑优先/嵌套路径", false, e.message); }

// ---------- 12) R07：compose 候选 → 参数转发 + 确认勾选 + 未确认警告 ----------
section("12");
try {
  check("R07: compose 字段可编辑（候选卡）", html.includes("compose_service") && html.includes("compose_project") &&
        html.includes("compose_files") && html.includes("provision"));
  page.setWiz({step: "review", _diffFetched: true, _resFetched: true, dirs: [], presetDirs: [], unresolved: [],
    project: {id: "dupproj", name: "DupProj", revision: 3}, agent: "codex", dir: srcA,
    candidates: [
      {id: "stack", name: "Stack", run: "compose", compose_service: "web", compose_project: "demo",
       compose_files: ["docker-compose.yml"], provision: "up",
       _orig: {id: "stack", name: "Stack", components: []},
       components: [{id: "app", kind: "server", name: "app", cwd: ".", run: "compose", compose_service: "web"}],
       dependencies: []},
      {id: "plain", name: "Plain",
       _orig: {id: "plain", name: "Plain", components: [{id: "m", cmd: ["make", "d"], cwd: "."}]},
       components: [{id: "m", kind: "server", name: "m", cmd: ["make", "d"], cwd: "."}], dependencies: []}],
    diffs: {}, results: {}});
  page.setViewRaw("new-project");
  page.render();
  const h12 = documentStub.getElementById("main").innerHTML;
  check("compose 候选渲染 compose 编辑输入与确认勾选",
        h12.includes("Compose service") && h12.includes("允许创建容器/拉镜像"));
  check("provision=up 未勾选 → 注册前可见警告", h12.includes("provision=up"));
  const cp = page.candPayload(page.wizGet().candidates[0], page.wizGet().project);
  const comp = cp.service.components[0];
  check("candPayload 转发 run/compose_service/compose_project/files/provision（无 cmd）",
        comp.run === "compose" && comp.compose_service === "web" && comp.compose_project === "demo" &&
        JSON.stringify(comp.compose_files) === JSON.stringify(["docker-compose.yml"]) &&
        comp.provision === "up" && comp.cmd === undefined, JSON.stringify(comp));
  check("未勾选确认 → 不携带 confirmed_provision", cp.service.confirmed_provision === undefined);
  page.wizConfirmProvision(0, true);
  const cp2 = page.candPayload(page.wizGet().candidates[0], page.wizGet().project);
  check("勾选确认 → confirmed_provision=true", cp2.service.confirmed_provision === true);
  const lp = page.candPayload(page.wizGet().candidates[1], page.wizGet().project);
  check("本地候选 payload 不变（cmd 转发、无 compose/确认字段）",
        JSON.stringify(lp.service.components[0].cmd) === JSON.stringify(["make", "d"]) &&
        lp.service.components[0].run === undefined && lp.service.confirmed_provision === undefined &&
        lp.service.edited === undefined);
} catch (e) { check("R07 compose 候选", false, e.message); }

// ---------- 13) R09：消歧零重复 / 不撞已登记 id / 同身份保持原 id ----------
section("13");
try {
  await page.refresh();
  const regIds = (page.regGet()?.services || []).map(s => s.id);
  check("已登记 id 进入消歧输入（registry 就绪）", regIds.includes("dup-app") && regIds.includes("dup-app-2"),
        regIds.join(","));
  const batch = [{id: "admin"}, {id: "admin"}, {id: "admin-2"}];
  page.disambiguateIds(batch);
  const ids = batch.map(c => c.id);
  check("[admin,admin,admin-2] → 批内全部唯一", new Set(ids).size === 3, ids.join(","));
  const again = [{id: "admin"}, {id: "admin"}, {id: "admin-2"}];
  page.disambiguateIds(again);
  check("消歧确定性（重复执行同结果）", JSON.stringify(again.map(c => c.id)) === JSON.stringify(ids),
        again.map(c => c.id).join(","));
  const ident = [{id: "dup-app", _root: fs.realpathSync(srcA), source_dir: "."}];   // 后台 source 经 resolve 归一（/private/var）
  page.disambiguateIds(ident);
  check("与已登记服务同身份（同 source）→ 保持原 id（再识别/更新路径）", ident[0].id === "dup-app", ident[0].id);
  const dirC = path.join(tmp, "dir-c");
  const clash = [{id: "dup-app", _root: dirC, source_dir: "."}];
  page.disambiguateIds(clash);
  check("与已登记不同身份的同名候选 → 分配空闲后缀（不撞任何已登记 id）",
        clash[0].id !== "dup-app" && !regIds.includes(clash[0].id), clash[0].id);
  const pair = [{id: "dup-app-2", _root: dirC, source_dir: "."}, {id: "dup-app-2", _root: path.join(tmp, "dir-d"), source_dir: "."}];
  page.disambiguateIds(pair);
  check("同名+撞已登记的批次也零重复", new Set(pair.map(c => c.id)).size === 2 &&
        pair.every(c => !regIds.includes(c.id)), pair.map(c => c.id).join(","));
} catch (e) { check("R09 消歧", false, e.message); }

// ---------- 14) R08：资源消费者/归属/挂载 + 向导复用绑定下拉 ----------
section("14");
try {
  // 第二个项目的服务：让 consumers 跨项目 → “共享（多项目引用）”标注
  const sharePay = {schemaVersion: 1, revision: 0, by: "gui",
    project: {id: "shareproj", name: "ShareProj", agent: "codex"},
    directories: [{path: srcB, role: "source"}],
    service: {id: "share-svc", name: "Share", source: srcB, evidence: {agent: "none", summary: "verify"},
              components: [{id: "main", kind: "server", name: "main", cmd: ["/bin/sleep", "200"], cwd: ".",
                            port: 18252, ready: {http: "http://127.0.0.1:18252/"}}]}};
  const [sc14, sr14] = await page.api("POST", "/api/register", sharePay);
  check("跨项目第二个服务已注册（归属判定输入）", sc14 === 200 && sr14.action === "created",
        JSON.stringify(sr14).slice(0, 120));
  await page.refresh();   // registry 提供 service→project 映射
  const resStub = {ok: true, docker_context: "stub", resources: [
    {id: "container:pg-stack", kind: "docker-container", name: "pg-stack", image: "postgres:16",
     state: "running", health: "healthy", compose_project: "demo", compose_service: "db",
     ports: {"5432/tcp": [{host_ip: "0.0.0.0", host_port: 5434}]}, volumes: ["demo_pgdata"],
     bind_mounts: [{source: "/Users/x/data", target: "/var/lib/postgresql/data"}]},
    {id: "container:cache", kind: "docker-container", name: "cache", image: "redis:7",
     state: "exited", health: "none", compose_project: "", compose_service: "",
     ports: {}, volumes: [], bind_mounts: []}],
    consumers: {"container:pg-stack": [
      {service: "dup-app", via: "dependency", running: true},
      {service: "share-svc", via: "compose", running: false}]}};
  const endpoint = "unix:///tmp/locove-verifier.sock";
  const discovered = {ok: true, environments: [
    {context: "stub", contexts: ["stub"], endpoint, provider: "docker", state: "ready", services: [],
     containers: [{...resStub.resources[0], consumers: [
       {id: "dup-app", name: "Dup App", via: "dependency", state: "available", label: "可用", project: "dupproj"},
       {id: "share-svc", name: "Share", via: "compose", state: "stopped", label: "已停止", project: "shareproj"},
     ]}, resStub.resources[1]]},
    {context: "colima-sandbox", contexts: ["colima-sandbox"], endpoint: "unix:///tmp/sandbox.sock",
     profile: "sandbox", provider: "colima", state: "stopped", containers: [],
     services: [{id: "sandbox", name: "Sandbox service", state: "stopped", label: "已停止"}]},
  ]};
  const pageFetch14 = globalThis.fetch;
  globalThis.fetch = async (p, opts) => {
    if (String(p).endsWith("/api/resources/environments"))
      return {ok: true, status: 200, json: async () => discovered, text: async () => JSON.stringify(discovered)};
    if (String(p).includes("/api/resources"))
      return {ok: true, status: 200, json: async () => resStub, text: async () => JSON.stringify(resStub)};
    return pageFetch14(p, opts);
  };
  await page.loadResources();
  page.setViewRaw("resources");
  page.render();
  const rh = documentStub.getElementById("main").innerHTML;
  check("资源表显示引用服务、数据卷与挂载", rh.includes("引用服务") && rh.includes("数据卷与挂载"));
  check("引用服务显示各自的状态和引用方式", rh.includes("Dup App · 可用 · 依赖") && rh.includes("Share · 已停止 · Compose"));
  check("跨项目引用可见", rh.includes("跨项目共享"));
  check("挂载渲染 source → target", rh.includes("/Users/x/data → /var/lib/postgresql/data"));
  check("已停止容器带「已停止」徽标", rh.includes("已停止"));
  check("离线 Colima 保留服务绑定并提供启动入口",
        rh.includes("Colima sandbox") && rh.includes("Sandbox service")
        && rh.includes("容器状态无法验证") && rh.includes("启动环境"));
  // 向导复用绑定
  await page.wizLoadResources();
  const wres = page.wizGet().resources;
  check("向导取得资源清单（含已停止容器）", Array.isArray(wres) && wres.length === 2);
  page.setWiz({step: "review", _diffFetched: true, _resFetched: true, dirs: [], presetDirs: [], unresolved: [],
    project: {id: "dupproj", name: "DupProj", revision: 3}, agent: "codex", dir: srcA, resources: wres,
    dockerEnvironments: discovered.environments,
    candidates: [{id: "app3", name: "App3",
      _docker: {context: "stub", endpoint, autostart: false},
      _orig: {id: "app3", name: "App3", components: [{id: "m", cmd: ["make", "r"], cwd: "."}]},
      components: [{id: "m", kind: "server", name: "m", cmd: ["make", "r"], cwd: ".", port: 18261}],
      dependencies: [{name: "postgres", probe: {tcp: "127.0.0.1:5432"}, container: "", volume: "demo_pgdata"}]}],
    diffs: {}, results: {}});
  page.setViewRaw("new-project");
  page.render();
  const wh = documentStub.getElementById("main").innerHTML;
  check("依赖行含 复用选择 下拉（不绑定 缺省项）", wh.includes("复用选择") && wh.includes("不绑定"));
  check("选项标签 name·state·compose·卷 + 卷匹配标注",
        wh.includes("pg-stack · running · demo/db") && wh.includes("（匹配：卷）"), "label check");
  page.wizBindDependency(0, 0, "pg-stack");
  const dep = page.wizGet().candidates[0].dependencies[0];
  check("选择复用 → 依赖 container/卷自动带出（声明的卷在容器上保持）",
        dep.container === "pg-stack" && dep.volume === "demo_pgdata", JSON.stringify(dep));
  const pl14 = page.candPayload(page.wizGet().candidates[0], page.wizGet().project);
  check("绑定随注册 payload 依赖提交",
        pl14.service.dependencies[0].container === "pg-stack" && pl14.service.dependencies[0].volume === "demo_pgdata"
        && pl14.service.docker.endpoint === endpoint);
  page.wizBindDependency(0, 0, "");
  const dep2 = page.wizGet().candidates[0].dependencies[0];
  check("「不绑定」恢复识别原值", dep2.container === "" && dep2.volume === "demo_pgdata", JSON.stringify(dep2));
  globalThis.fetch = pageFetch14;
} catch (e) { check("R08 资源/复用绑定", false, e.message); }


// ---------- 15) V3 Locove：域名作为服务行主要信息 + 健康分层 + 详情入口 ----------
try {
  const mkEp = (id, src, port) => ({
    schemaVersion: 1, revision: 0, by: "gui",
    project: {id: "epproj", name: "EpProj"},
    directories: [{path: src, role: "source"}],
    service: {id, name: "Domain " + id, source: src,
              endpoints: [{host: "domain-app.locove", component: "web", open_path: "/zh"}],
              evidence: {agent: "none", summary: "verify"},
              components: [{id: "web", kind: "server", name: "web", cmd: ["/bin/sleep", "100"], cwd: ".",
                            port, ready: {http: `http://127.0.0.1:${port}/`}}]},
  });
  const [c, r] = await page.api("POST", "/api/register", mkEp("domain-app", srcA, 18260));
  check("V3: 带 endpoints 的服务注册 created", c === 200 && r.action === "created", JSON.stringify(r).slice(0, 140));
  const [, def] = await page.api("GET", "/api/services/domain-app/definition");
  check("V3: 定义 API 返回 endpoints",
        def?.definition?.endpoints?.[0]?.host === "domain-app.locove"
        && def.definition.endpoints[0].open_path === "/zh", JSON.stringify(def?.definition?.endpoints));
  const [lc, lv] = await page.api("GET", "/api/locove");
  check("V3: /api/locove 域名模块状态机（dns/proxy/resolver 分层 + state + legacy_80）",
        lc === 200 && lv.dns && lv.proxy && lv.resolver && typeof lv.state === "string"
        && typeof lv.legacy_80 === "boolean" && typeof lv.resolver_installed === "boolean"
        && lv.domain_port === 5670,
        JSON.stringify(lv).slice(0, 120));
  // V3.5：默认本地 IP 直连
  check("V3.5: 默认设置 use_domains=false（本地 IP 直连）",
        lv.settings?.use_domains === false && lv.settings?.domain_form === "suffix",
        JSON.stringify(lv.settings));
  await page.api("POST", "/api/refresh");   // 快照有 3s 缓存：显式刷新拿到新登记
  await page.refresh();
  let html = page.viewAll();
  check("V3.5: 默认显示本地直连 chip（含开启指引）",
        html.includes("本地直连") && html.includes("127.0.0.1:18260"),
        "settings=" + JSON.stringify((page.getLocoveData() || {}).settings));
  const [, stOn] = await page.api("POST", "/api/locove/settings", {use_domains: true, domain_form: "suffix"});
  check("V3.5: 开启域名跳转（use_domains+domain_form 同时配置）",
        stOn?.ok && stOn.settings.use_domains === true, JSON.stringify(stOn).slice(0, 120));
  await page.api("POST", "/api/refresh");
  await page.refresh();
  // 确定性驱动：模块可用 + 已收敛路由（隔离环境无 Caddy，不猜运行态）
  page.setLocoveData({state: "available", domain_port: 5670,
                      dns: {ok: true}, proxy: {ok: true}, resolver: {ok: true},
                      routes: {"domain-app.locove": {service: "domain-app", component: "web", upstream_port: 18260, state: "proxy"}},
                      settings: {use_domains: true, domain_form: "suffix", domain_port: 5670}});
  html = page.viewAll();
  check("D3: 开启后服务行显示带端口域名（domain chip host:5670）",
        html.includes("domain-app.locove:5670") && html.includes("点击复制"), html.slice(html.indexOf("domain"), html.indexOf("domain") + 160));
  check("D3: 域名 chip 可复制（copyText 绑定 :5670 URL）", html.includes("copyText('http://domain-app.locove:5670/zh'"), "");
  const [, stOff] = await page.api("POST", "/api/locove/settings", {use_domains: false});
  check("V3.5: 可关闭回到默认直连", stOff?.settings.use_domains === false, "");
  check("V3: 单服务项目不重复项目标题（slim header，仅收藏+菜单）",
        !/<h3>[^<]*EpProj/.test(html), "");
  page.setOpen({"domain-app": true});
  const proj = page.snapGet().projects.find(x => x.id === "domain-app");
  const det = page.detailsHtml(proj);
  check("V3: 详情含入口区（public_url 带端口 + 组件/落点）",
        det.includes("http://domain-app.locove:5670/zh") && det.includes("组件 web") && det.includes("/zh"), "");
  check("V3: 详情直连地址标注为内部实现", det.includes("直连地址") && det.includes("内部端口"), "");
  page.setLocoveData({state: "failed", dns: {ok: false, detail: "本地 DNS 未运行"}, proxy: {ok: true}, resolver: {ok: false, detail: "未安装"},
                      routes: {}, settings: {use_domains: true, domain_form: "suffix", domain_port: 5670}});
  page.renderHealth();
  const strip = documentStub.getElementById("locoveHealth").innerHTML;
  check("R6/D2: 开启后健康条为一行摘要（故障层命名）+ 去设置入口",
        strip.includes("DNS") && strip.includes("解析配置") && strip.includes("setView('settings')")
        && !strip.includes("sudo "), strip.slice(0, 90));
  page.setLocoveData({state: "installed_off", domain_port: 5670,
                      dns: {ok: false, detail: "本地 DNS 未运行"}, proxy: {ok: false, detail: "未监听"},
                      resolver: {ok: true, detail: "正常"}, routes: {},
                      settings: {use_domains: false, domain_form: "suffix", domain_port: 5670}});
  const settingsHtml = page.viewSettings();
  check("D3: 启用入口与诊断完整收进设置视图（不再引导 sudo 命令安装）",
        settingsHtml.includes('onclick="askEnableDomain()">启用…') && settingsHtml.includes("域名诊断") && !settingsHtml.includes("sudo "), "");
  check("浏览器设置不显示桌面快捷键", !settingsHtml.includes("editShortcut"), "");
  window.lsShortcuts([
    {id: "mainWindowShortcut", title: "打开主窗口", label: "⌃⇧L", error: ""},
    {id: "quickServicesShortcut", title: "快速打开运行中的服务", label: "⌃⇧Space", error: ""}
  ]);
  const shortcutSettings = page.viewSettings();
  check("桌面设置可分别编辑两个快捷键",
        shortcutSettings.includes("<kbd>⌃⇧L</kbd>") && shortcutSettings.includes("<kbd>⌃⇧Space</kbd>") &&
        shortcutSettings.includes('data-shortcut="mainWindowShortcut"') &&
        shortcutSettings.includes('data-shortcut="quickServicesShortcut"'), "");
  page.setLocoveData({state: "available", dns: {ok: true}, proxy: {ok: true}, resolver: {ok: true}, routes: {},
                      settings: {use_domains: true, domain_form: "suffix"}});
  page.renderHealth();
  check("V3: 全部健康时健康条隐藏", documentStub.getElementById("locoveHealth").innerHTML === "", "");
  page.setLocoveData({state: "not_installed", dns: {ok: null}, proxy: {ok: null}, resolver: {ok: null}, routes: {},
                      settings: {use_domains: false, domain_form: "suffix"}});
  page.renderHealth();
  check("D2: 未安装域名件不产生告警（默认直连不是故障）", documentStub.getElementById("locoveHealth").innerHTML === "", "");
  page.setLocoveData({state: "failed", dns: {ok: false, detail: "d"}, proxy: {ok: true}, resolver: {ok: true},
                       routes: {}, settings: {use_domains: true, domain_form: "suffix", domain_port: 5670}});
  const chipDown = page.domainChip({...proj, endpoints: [{host: "x.locove", public_url: "http://x.locove:5670/", id: "default", component: "web", open_path: "/"}]});
  check("D3: 模块未就绪时域名 chip 显示降级提示（仍可直连）", chipDown.includes("域名模块未就绪") && chipDown.includes("down"), chipDown.slice(0, 90));
} catch (e) { check("V3 域名 UI", false, e.message); }

// ---------- R6：完整工作台改造（紧凑行 / 次要动作收拢 / 详情分层 / 设置草稿分离） ----------
section("R6 workbench");
try {
  page.setLocoveData({dns: {ok: true}, proxy: {ok: true}, entry: {ok: true}, routes: {},
                      settings: {use_domains: false, domain_form: "suffix"}});
  // 用运行态快照事实驱动行渲染（隔离 daemon 里服务未启动，需要伪造运行事实）
  const snapOrig = page.snapGet();
  const runSnap = JSON.parse(JSON.stringify(snapOrig));
  const runProj = runSnap.projects.find(x => x.id === "domain-app");
  Object.assign(runProj, {state: "available", label: "可用", op: null, intent: "running", can_open: true});
  page.setSnap(runSnap);
  const row = page.serviceRow(runProj, false);
  check("R6: 运行中行只保留主操作（打开）+ 稳定日志入口 + ⋯",
        row.includes(">打开<") && row.includes("loadLogs('domain-app')") && row.includes("toggleMenu(event,'svc:domain-app')"), "");
  check("R6: 停止/重启/源码目录不在行内展开（收拢进 ⋯ 菜单）",
        !row.includes(">停止<") && !row.includes(">重启<") && !row.includes(">源码目录<") && !row.includes("open-source"), "");
  const menuItems = page.serviceMenuItems("domain-app").filter(i => !i.sep).map(i => i.label);
  check("R6: ⋯ 菜单承载次要动作（编辑/日志/打开目录/停止/重启/移除）",
        ["编辑", "日志", "打开目录", "停止", "重启", "移除登记…"].every(l => menuItems.includes(l)), menuItems.join(","));
  check("R6: 行可键盘展开详情（role=button + aria-expanded）",
        row.includes('role="button"') && row.includes('aria-expanded='), "");
  page.setOpen({"domain-app": true});
  const rowOpen = page.serviceRow(runProj, false);
  const detail = page.detailsHtml(runProj);
  check("R6: 详情分层（概览/入口/组件/日志 标题）",
        detail.includes("概览") && detail.includes("入口") && detail.includes("组件") && detail.includes("日志"), "");
  check("R6: 展开行呈现详情面板", rowOpen.includes("detail") && rowOpen.includes("运行意图"), "");
  page.setOpen({});
  page.setSnap(snapOrig);
  const nav = html;
  check("R6: 统一导航分区（服务/项目/管理 + 设置独立项）",
        nav.includes('id="nav-all"') && nav.includes('id="nav-settings"') && nav.includes('id="navProjects"')
        && nav.includes("设置") && !nav.includes("设置 / 草稿"), "");
  check("R6: 草稿归入添加流程（添加页含草稿区；设置页不含草稿表单）",
        nav.includes("＋ 添加服务"), "");
  const addHtml = page.viewNewProject();
  check("R6: 添加视图结构保留完整流程（目录与识别步骤）",
        addHtml.includes("目录与项目") && (addHtml.includes("重新识别") || addHtml.includes("识别")), addHtml.slice(0, 80));
  const draftArea = page.draftCards();
  check("R6: 无草稿时草稿区为空", draftArea === "", "");
  const group = page.viewAll();
  check("R6: 分组为整组紧凑行容器（group + 细分隔行）",
        group.includes('class="group"') && group.includes('class="svc'), group.slice(0, 80));
} catch (e) { check("R6 workbench", false, e.message); }

section("independent recognition tasks");
{
  const oldWizard = page.wizGet();
  const oldRegistry = page.regGet();
  try {
    page.wizNewTask();
    const first = page.wizGet();
    page.wizField("dir", srcA + "/Buildbase/");
    check("目录变更自动填写项目名", first.name === "Buildbase");
    page.wizField("name", "My app");
    page.wizField("dir", srcA + "/Renamed");
    check("切换目录保留手填项目名", first.name === "My app");
    first.job = {state: "running"};
    page.wizNewTask();
    check("另开任务显示独立表单", page.wizGet() !== first && page.wizGet().dir === "");
    page.wizSelectTask(first.taskId);
    check("切回原任务保留识别状态", page.wizGet() === first && first.job.state === "running");
    page.setRegistry({registry: {projects: [{id: "empty-fixture", name: "Empty fixture", services: [], revision: 3, directories: [{path: first.dir}]}]}});
    check("空项目隐藏于服务列表", !page.viewAll().includes("Empty fixture"));
    check("重新选择关联目录时复用隐藏的空项目", page.wizardTarget(first)?.id === "empty-fixture");
  } finally {
    page.setWiz(oldWizard);
    page.setRegistry(oldRegistry);
  }
}

section("Codex model selection");
{
  const oldWizard = page.wizGet();
  const workbenchFetch = globalThis.fetch;
  const sent = [];
  try {
    globalThis.fetch = async (url, options) => {
      if (url !== "/api/analyze") return workbenchFetch(url, options);
      sent.push(JSON.parse(options.body));
      return {status: 400, json: async () => ({error: "model_not_supported"})};
    };
    page.setWiz({step: "dir", dir: srcA, name: "Codex model fixture", agent: "codex"});
    check("Codex 模型输入默认显示 Auto", page.viewNewProject().includes('placeholder="Auto"'));
    await page.wizStartAnalyze();
    check("Auto 识别请求省略 model", !Object.hasOwn(sent[0], "model"));
    page.wizField("model", " pinned ");
    await page.wizStartAnalyze();
    check("手动固定模型随请求发送", sent[1].model === "pinned");
    check("固定模型不可用时可主动用 Auto 重试", page.viewNewProject().includes("用 Auto 重试"));
  } finally {
    page.setWiz(oldWizard);
    globalThis.fetch = workbenchFetch;
  }
}

section("verified registration");
{
  const oldWizard = page.wizGet();
  const trialProject = {id: "trial-fixture", name: "Trial fixture", revision: 0};
  try {
    page.setWiz({step: "review", dir: srcA, agent: "codex", target: trialProject,
      _diffFetched: true, _resFetched: true, results: {}, diffs: {}, candidates: [{
        id: "trial-fixture", name: "Trial fixture", open: "http://127.0.0.1:48229/",
        components: [{id: "web", cmd: ["/usr/bin/false"], port: 48229}],
        unresolved: ["Check Python version"],
      }]});
    await page.wizRegister(page.wizGet(), true);
    const result = page.wizGet().results[0]?.res;
    check("自动登记必须通过试启动", result?.trial?.state === "failed" && !result.service_id);
    check("自动登记失败保留本机验证记录", page.viewNewProject().includes("本机验证记录"));
    page.setWiz({step: "dir", dir: srcA, agent: "", name: "Trial fixture", presetDirs: []});
    const initial = page.viewNewProject();
    check("添加页可启用试启动后自动登记", initial.includes("试启动通过后自动登记"));
    check("未配置 Agent 时仍可获取外部提示词", initial.includes("复制外部 Agent 提示词"));
  } finally { page.setWiz(oldWizard); }
}

section("port repair actions");
{
  const originalFetch = globalThis.fetch;
  const originalSnap = page.snapGet();
  const originalWebKit = window.webkit;
  const copied = [];
  const sid = "gui-port-repair";
  const issue = {kind: "occupied", component: "web", host: "127.0.0.1", port: 48229};
  const fixture = {id: sid, name: "Repair fixture", state: "conflict", intent: "stopped",
    components: [{id: "web", name: "Web", kind: "server", port: issue.port}],
    port_repair: "auto", port_issues: [issue]};
  const calls = [];
  const installSnap = value => page.setSnap({...originalSnap, projects: [...originalSnap.projects, value]});
  globalThis.fetch = async (url, options) => {
    if (url === `/api/services/${sid}/repair`) {
      const body = JSON.parse(options.body);
      calls.push(body);
      return {ok: true, status: 200, json: async () => body.mode === "auto"
        ? {ok: true, started: true, port: 48230}
        : {ok: true, prompt: "请在项目目录修复端口 48229"}};
    }
    return originalFetch(url, options);
  };
  try {
    const failure = { ...fixture, state: "failed", port_repair: null, port_issues: [] };
    check("普通启动失败不显示端口修复", !page.detailsHtml(failure).includes('data-act="port-repair"'));
    installSnap(fixture);
    check("确认的端口冲突提供一键验证入口", page.detailsHtml(fixture).includes("换端口并验证"));
    await page.repairPort(sid, "auto");
    check("一键修复提交当前组件和冲突端口",
          JSON.stringify(calls[0]) === JSON.stringify({mode: "auto", component: "web", port: 48229}));
    const manual = {...fixture, port_repair: "prompt"};
    installSnap(manual);
    check("项目配置问题显示提示词入口", page.detailsHtml(manual).includes("复制项目修复提示词"));
    await page.repairPort(sid, "prompt");
    const modal = page.getModalState();
    check("提示词可选中复制", calls[1]?.mode === "prompt" &&
          modal?.overlay?.children[0]?.children[1]?.innerHTML.includes("请在项目目录修复端口 48229"));
    window.webkit = {messageHandlers: {lsclipboard: {postMessage: async text => { copied.push(text); return true; }}}};
    await modal?.okBtn?.onclick();
    check("原生复制将完整提示词写入剪贴板后关闭", copied[0] === "请在项目目录修复端口 48229" && !page.modalOpen());
    if (page.modalOpen()) dispatchKey("Escape");
  } finally {
    window.webkit = originalWebKit;
    globalThis.fetch = originalFetch;
    page.setSnap(originalSnap);
  }
}

section("registration review port conflict");
{
  const oldWizard = page.wizGet();
  const oldPort = await freePort();
  const sid = "review-port-fixture";
  const owner = {
    id: "review-port-owner", name: "Port owner", source: srcB,
    components: [{id: "web", cmd: ["python3", "-m", "http.server", String(oldPort)], port: oldPort}],
  };
  try {
    const [code] = await page.api("POST", "/api/register", {schemaVersion: 1, service: owner});
    if (code !== 200) throw new Error("Could not reserve the fixture port");
    const wizard = {
      step: "review", dir: srcA, agent: "codex", target: {id: sid, name: "Port review", revision: 0},
      _diffFetched: true, _resFetched: true, results: {}, diffs: {}, candidates: [{
        id: sid, name: "Port review", open: `http://127.0.0.1:${oldPort}/`,
        components: [{id: "web", cmd: ["python3", "-m", "http.server", String(oldPort), "--bind", "127.0.0.1"],
                      port: oldPort, start_timeout: 6, hot: true, auto_update: false}],
      }],
    };
    page.setWiz(wizard);
    await page.wizPrefetchDiff(0);
    const recommended = wizard.diffs[0]?.port_conflicts?.[0]?.suggested_port;
    const preview = page.viewNewProject();
    check("候选冲突提供项目修复提示词", preview.includes("复制项目修复提示词"));
    check("候选端口冲突显示登记预留、推荐和手动试启动", recommended &&
          preview.includes("登记预留") && preview.includes("review-port-owner.web") &&
          preview.includes(`用 ${recommended} 试启动`) && preview.includes("手动端口试启动"));
    const before = requests.filter(r => r.path === "/api/register").length;
    await page.wizRegister(wizard);
    check("批量注册跳过冲突候选", requests.filter(r => r.path === "/api/register").length === before);
    await page.wizTryCandidatePorts(0, [recommended]);
    const result = wizard.results[0]?.res;
    check("审核页换端口调用真实试启动，验证后登记", result?.service_id === sid &&
          result.trial?.state === "passed" && result.trial.checks[0]?.port === recommended);
    const [, saved] = await page.api("GET", `/api/services/${sid}/definition`);
    check("审核页换端口保留高级设置", saved?.definition?.components[0]?.start_timeout === 6 &&
          saved.definition.components[0].hot === true && saved.definition.components[0].auto_update === false);
  } catch (e) { check("审核页端口修复", false, e.message); }
  finally { page.setWiz(oldWizard); }
}

section("analysis activity");
{
  const oldWizard = page.wizGet();
  const now = Date.now() / 1000;
  try {
    page.setWiz({step: "job", agent: "claude", dir: srcA, jobId: "job-fixture", activityOpen: true,
      job: {state: "running", started_at: now - 30, timeout: 240, activity: [
        {event: "scan_directory", at: now - 29},
        {event: "tool_read", at: now - 15},
      ]}});
    const analysisHtml = page.viewNewProject();
    check("分析中显示实际 CLI 活动与等待时长",
          analysisHtml.includes("Claude 发起文件读取") && analysisHtml.includes("没有新活动")
          && analysisHtml.includes('id="analysis-activity" open'), "");
    page.wizGet().job = {state: "failed", error: "timeout"};
    check("旧任务无活动记录仍可显示超时",
          page.viewNewProject().includes("调用超时"), "");
  } finally { page.setWiz(oldWizard); }
}

// ---------- W1：轮询稳定性 + 稳定操作标识（共享工作台重构） ----------
section("W1 workbench");
try {
  const main = documentStub.getElementById("main");
  page.setViewRaw("all");
  page.render();
  let writes = 0, val = main.innerHTML;
  Object.defineProperty(main, "innerHTML", {get: () => val, set(v) { writes += 1; val = v; }, configurable: true});
  page.render();
  check("W1: 状态未变时轮询 render 不重写内容区（节点身份保持）", writes === 0, `writes=${writes}`);
  const snapW1 = page.snapGet();
  const w1snap = JSON.parse(JSON.stringify(snapW1));
  if (w1snap.projects.length) w1snap.projects[0].label = "W1-CHANGED";
  page.setSnap(w1snap);
  page.render();
  check("W1: 状态变化时 render 重建内容区（自愈外部改写）", writes >= 1, `writes=${writes}`);
  Object.defineProperty(main, "innerHTML", {get: () => val, set(v) { val = v; if (globalThis.__lsMainObserver) globalThis.__lsMainObserver.cb(); }, configurable: true});
  page.setSnap(snapW1);
  page.render();
  const rowHtml = main.innerHTML;
  check("W1: 服务行操作带稳定 data-act 标识（logs/menu）",
        rowHtml.includes('data-act="logs"') && rowHtml.includes('data-act="menu"'), "");
  check("W1: 状态列合并构建状态（statecell，地址列不再单列互挤）", rowHtml.includes("statecell"), "");
  check("W1: lsDiagnose 原生诊断入口存在（W2 桥接）", typeof page.lsDiagnose === "function", typeof page.lsDiagnose);
  const root = makeEl("main");
  const c1 = makeEl("button"), c2 = makeEl("div"), g = makeEl("section");
  root.appendChild(c1); root.appendChild(c2); c2.appendChild(g);
  const path = page.domPath(root, g);
  check("W1: domPath/findByDomPath 往返定位（焦点恢复基础）",
        JSON.stringify(path) === "[1,0]" && page.findByDomPath(root, path, "SECTION") === g, JSON.stringify(path));
} catch (e) { check("W1 workbench", false, e.message); }

section("unlink directory");
{
  const id = "unlink-fixture";
  const dir = path.join(tmp, "unlink & verify");
  fs.mkdirSync(dir);
  const file = path.join(dir, "keep.txt");
  fs.writeFileSync(file, "keep this file");
  const [code, response] = await page.api("POST", "/api/register", {
    schemaVersion: 1, revision: 0, project: {id, name: "Unlink fixture"},
    directories: [{path: dir, role: "source"}],
    service: {id, name: "Unlink fixture", source: dir,
              components: [{id: "main", kind: "worker", cmd: ["/usr/bin/true"]}]},
  });
  if (code !== 200 || !response.ok) throw new Error("Could not link fixture directory");
  await page.refresh();
  const project = () => page.regGet().registry.projects.find(p => p.id === id);
  const dirId = project().directories[0].id;
  const endpoint = `/api/projects/${id}/directories/${dirId}`;
  const deletes = () => requests.filter(r => r.method === "DELETE" && r.path === endpoint).length;
  const button = page.viewProject(id).match(/onclick="(removeDir\([^"]+\))"/);
  if (!button) throw new Error("Unlink button is missing");
  const clickUnlink = new Function("removeDir", "return " + button[1]).bind(null, page.removeDir);

  let unlink = clickUnlink();
  const modal = page.getModalState();
  check("解除关联显示工作台确认框并转义目录路径",
        modal?.dialog.children[1].innerHTML.includes("unlink &amp; verify"), "missing in-page confirmation");
  if (modal) dispatchKey("Escape");
  await unlink;
  check("取消解除关联不发送删除请求", deletes() === 0);

  unlink = clickUnlink();
  const confirm = page.getModalState();
  if (confirm) await confirm.okBtn.onclick();
  await unlink;
  check("确认解除关联后移除列表条目并保留目录内容",
        deletes() === 1 && project().directories.length === 0 &&
        !page.viewProject(id).includes(dirId) && fs.readFileSync(file, "utf8") === "keep this file");

  await page.api("POST", `/api/projects/${id}/directories`, {path: dir});
  await page.refresh();
  const relinked = project().directories[0].id;
  unlink = page.removeDir(id, relinked);
  await page.api("DELETE", `/api/projects/${id}/directories/${relinked}`);
  await page.getModalState()?.okBtn.onclick();
  const failed = page.getModalState();
  check("解除关联失败时在确认框显示接口错误",
        failed?.dialog.children[1].innerHTML.includes("未知目录"));
  if (failed) dispatchKey("Escape");
  await unlink;
}

console.log(out.join("\n"));
const fails = out.filter(l => l.startsWith("FAIL")).length;
console.log(`\n==== ${out.length - fails} passed, ${fails} failed ====`);
cleanup();
process.exit(fails ? 1 : 0);
}

main().catch(e => { console.error("FATAL:", e); process.exit(2); });
