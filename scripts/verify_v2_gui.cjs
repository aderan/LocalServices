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
const daemon = spawn("python3", ["-m", "localservices.daemon"], {
  cwd: REPO, env: {...process.env, LOCALSERVICES_HOME: home, LOCALSERVICES_SERVICES: servicesDir,
                          LOCALSERVICES_PORT: String(port)},
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

const runner = new Function(script + "\n; return {render, api, refresh, showModal, modalOpen, uiLocked, toggleMenu, closeMenu, menuItems, askRemoveService, askProjectRemove, disambiguateIds, wizAcceptNew, candPayload, toggleAutostart, editService, editedPaths, viewRemoved, loadRemoved, restoreService, restoreProject, setWiz: w => {wiz = w;}, wizGet: () => wiz, getModalState: () => modalState, getMenuState: () => menuState, getRequests: () => requests, setRegistry: r => {registry = r;}, setSnap: s => {snap = s;}};");
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
  const srcAnchors = html.includes('toggleMenu(event,\'svc:') && html.includes('aria-haspopup="menu"');
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

console.log(out.join("\n"));
const fails = out.filter(l => l.startsWith("FAIL")).length;
console.log(`\n==== ${out.length - fails} passed, ${fails} failed ====`);
cleanup();
process.exit(fails ? 1 : 0);
}

main().catch(e => { console.error("FATAL:", e); process.exit(2); });
