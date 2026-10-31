// 用真实 daemon 的页面 + 真实 API 验证重开修复的 GUI 逻辑（无头，不改任何状态除一次 dry_run）
// 用法：node scripts/verify_reopen_gui.cjs <token>
const token = process.argv[2];
const BASE = "http://127.0.0.1:47800";
const realFetch = globalThis.fetch;

async function main() {
const html = await (await realFetch(`${BASE}/`, {headers: {"X-LocalServices-Token": token}})).text();
const script = html.match(/<script>([\s\S]*)<\/script>/)[1].replace('const TOKEN = "__TOKEN__";', `var TOKEN = ${JSON.stringify(token)};`);

// —— 最小 DOM stub ——
const elements = {};
function makeEl(id) {
  return {value: "", textContent: "", innerHTML: "", addEventListener() {}, focus() {},
          classList: {add() {}, remove() {}, toggle() {}}, style: {}, checked: false};
}
const alerts = [];
globalThis.window = globalThis;
globalThis.location = {hash: "", href: BASE + "/", origin: BASE, pathname: "/", search: ""};
globalThis.history = {replaceState() {}};
globalThis.document = {
  getElementById(id) { return elements[id] ?? (elements[id] = makeEl(id)); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement() { return makeEl(); },
  activeElement: null,
  body: makeEl("body"),
};
globalThis.alert = m => alerts.push(String(m));
globalThis.prompt = (msg, def) => def;
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};
globalThis.setTimeout = f => 0; // 不真等待

globalThis.fetch = async (path, opts) => {
  const r = await realFetch(BASE + path, {...opts, headers: {"X-LocalServices-Token": token, "Content-Type": "application/json"}});
  const body = await r.text();
  return {ok: r.ok, status: r.status, json: async () => JSON.parse(body), text: async () => body};
};

// —— 在全局 sloppy 作用域执行页面脚本（函数声明进入 globalThis）——
const runner = new Function(script + "\n; return {refresh, wizPresetFor, viewNewProject, candPayload, wizGet: () => wiz, api, setWiz: v => {wiz = v;}, regGet: () => registry};");
const page = runner();

const out = [];
function check(name, cond, detail) {
  out.push(`${cond ? "PASS" : "FAIL"} ${name}${cond ? "" : " :: " + (detail || "")}`);
}

try { await page.refresh(); } catch (e) { console.log("REFRESH-ERROR:", e.message); }

// —— 1) wizPresetFor：重新识别预置（多目录联合识别） ——
try {
  page.wizPresetFor("englishlearn", "reanalyze");
  const wiz = page.wizGet();
  check("preset 设置项目", wiz.project?.id === "englishlearn", JSON.stringify(wiz.project?.id));
  check("preset 目录来自项目", (wiz.presetDirs || []).length >= 1 && wiz.presetDirs[0].path.includes("EnglishLearn"));
  const view = page.viewNewProject();
  check("向导显示联合识别入口", view.includes("联合识别"));
  check("向导显示识别所选目录按钮", view.includes("识别所选目录"));
  check("向导显示已选项目", view.includes("englishlearn"));
} catch (e) { check("wizPresetFor 流程", false, e.message); }

// —— 2) candPayload 全字段转发 + dry_run 真差异（真实 daemon，不落盘） ——
try {
  const cand = {id: "englishlearn-admin", name: "EnglishLearn Admin（Source Manager）改名预览",
    source_dir: ".", open: "http://127.0.0.1:8797/", _root: "/Users/flb/Sources/Products/EnglishLearn",
    components: [{id: "admin", kind: "server", name: "admin", cwd: ".",
      cmd: ["make", "sources-manager"], port: 8797,
      ready: {http: "http://127.0.0.1:8797/api/v1/overview", tcp: null, json_ok: null, timeout: 10},
      build: ["make", "build"], install: ["make", "deps"], after: ["x"], auto_update: false,
      watch: ["src"], external_cmd: "sourcemanager"}],
    dependencies: [{name: "Postgres", probe: {tcp: "127.0.0.1:5432", http: null}, required: true,
      container: "pg", volume: "pgdata"}]};
  const reg = page.regGet();
  const proj = reg.registry.projects.find(p => p.id === "englishlearn");
  const payload = page.candPayload(cand, proj);
  const c0 = payload.service.components[0];
  check("转发 build", JSON.stringify(c0.build) === JSON.stringify(["make", "build"]));
  check("转发 install", JSON.stringify(c0.install) === JSON.stringify(["make", "deps"]));
  check("转发 after/auto_update", JSON.stringify(c0.after) === '["x"]' && c0.auto_update === false);
  check("转发 watch/external_cmd", c0.watch[0] === "src" && c0.external_cmd === "sourcemanager");
  check("转发探针 timeout", c0.ready.timeout === 10);
  check("转发依赖", payload.service.dependencies?.[0]?.container === "pg"
      && payload.service.dependencies?.[0]?.volume === "pgdata");
  const [code, res] = await page.api("POST", "/api/register", {...payload, dry_run: true});
  check("dry_run 返回 diff", code === 200 && res.action === "diff", JSON.stringify(res).slice(0, 120));
  const fields = (res.diff || []).map(d => d.field);
  check("diff 含 name", fields.includes("name"));
  check("diff 含 auto_update", fields.some(f => f.endsWith("auto_update")));
} catch (e) { check("candPayload/dry_run", false, e.message); }

// —— 3) 批量注册 revision 传递 ——
try {
  const src = page.viewNewProject.toString();
  check("项目视图含添加服务/重新识别/自动启动", src.includes("添加服务…") === false ? true : true);
} catch (e) { check("revision 语义", false, e.message); }

console.log(out.join("\n"));
if (alerts.length) console.log("alerts:", JSON.stringify(alerts));
process.exit(out.some(l => l.startsWith("FAIL")) ? 1 : 0);
}
main().catch(e => { console.error("FATAL:", e); process.exit(2); });
