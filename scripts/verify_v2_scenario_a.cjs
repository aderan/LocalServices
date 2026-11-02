// ShiCha 场景 A：GUI 代码路径执行「移除服务登记」（预览→取消→零变化→实际移除）。
// 用法：node scripts/verify_v2_scenario_a.cjs <token>   （对真实 47800 daemon）
const token = process.argv[2];
const BASE = "http://127.0.0.1:47800";
const realFetch = globalThis.fetch;
const out = [];
const check = (name, cond, detail) => out.push(`${cond ? "PASS" : "FAIL"} ${name}${cond ? "" : " :: " + (detail || "")}`);

function makeEl(tag) {
  const el = {tagName: (tag || "div").toUpperCase(), children: [], value: "", textContent: "",
    innerHTML: "", className: "", style: {}, disabled: false, type: "", checked: false,
    attrs: {}, onclick: null,
    setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; },
    appendChild(c) { this.children.push(c); return c; },
    remove() {}, focus() {}, click() { if (this.onclick) this.onclick(); },
    addEventListener() {}, removeEventListener() {},
    querySelectorAll() { return []; }, querySelector() { return null; },
    classList: {add() {}, remove() {}, toggle() {}}};
  return el;
}
const elements = {};
globalThis.window = globalThis;
globalThis.location = {hash: "", href: BASE + "/", origin: BASE, pathname: "/", search: ""};
globalThis.history = {replaceState() {}};
globalThis.document = {
  getElementById(id) { return elements[id] ?? (elements[id] = makeEl()); },
  querySelector() { return null; }, querySelectorAll() { return []; },
  createElement(t) { return makeEl(t); },
  activeElement: null, body: makeEl("body"),
  addEventListener() {}, removeEventListener() {},
};
globalThis.alert = m => out.push("ALERT: " + String(m));
globalThis.prompt = () => null;
globalThis.confirm = () => false;
globalThis.setInterval = () => 0; globalThis.clearInterval = () => {};
globalThis.fetch = async (path, opts) => {
  const r = await realFetch(BASE + path, {...opts, headers: {"X-LocalServices-Token": token, "Content-Type": "application/json", ...(opts?.headers || {})}});
  const body = await r.text();
  return {ok: r.ok, status: r.status, json: async () => JSON.parse(body), text: async () => body};
};

const sleep = ms => new Promise(r => setTimeout(r, ms));
async function tick() { await sleep(5); }

async function main() {
  const html = await (await realFetch(`${BASE}/`, {headers: {"X-LocalServices-Token": token}})).text();
  const script = html.match(/<script>([\s\S]*)<\/script>/)[1]
      .replace('const TOKEN = "__TOKEN__";', `var TOKEN = ${JSON.stringify(token)};`);
  const runner = new Function(script + `\n; return {
    refresh, askRemoveService, servicesById, regGet: () => registry,
    modal: () => (typeof modalState !== "undefined" ? modalState : null),
    getDisp: () => __rmDisp, setDisp: v => { __rmDisp = v; } };`);
  const page = runner();

  await page.refresh();
  const before = JSON.stringify(page.regGet());
  check("初始状态含 shicha 服务", !!page.servicesById()["shicha"]);

  // —— 第一次：打开模态 → 取消（零副作用） ——
  const p1 = page.askRemoveService("shicha");
  for (let i = 0; i < 200 && !page.modal(); i++) await sleep(25);
  const m1 = page.modal();
  check("移除模态已打开", !!m1, "modalState 为空");
  const bodyHtml = m1 ? m1.dialog.innerHTML : "";
  check("模态显示对象名称", bodyHtml.includes("ShiCha"), bodyHtml.slice(0, 200));
  check("模态显示保留内容（容器/卷）", bodyHtml.includes("shicha-postgres") && bodyHtml.includes("shicha_postgres_data"));
  check("模态显示恢复方式", bodyHtml.includes("恢复方式"));
  check("模态提供处置选择", bodyHtml.includes("停止后移除") && bodyHtml.includes("保留为外部实例"));
  m1.cancelBtn.onclick();
  const ok1 = await p1;
  check("取消路径 resolve(false)", ok1 === false);
  await page.refresh();
  check("取消后登记零变化", JSON.stringify(page.regGet()) === before);
  check("取消后 shicha 仍在", !!page.servicesById()["shicha"]);

  // —— 第二次：实际移除（处置=停止后移除） ——
  const p2 = page.askRemoveService("shicha");
  for (let i = 0; i < 200 && !page.modal(); i++) await sleep(25);
  const m2 = page.modal();
  check("模态再次打开", !!m2);
  page.setDisp("stop");
  check("处置选择=停止后移除", page.getDisp() === "stop");
  m2.okBtn.onclick();
  const ok2 = await p2;
  check("确认路径 resolve(true)", ok2 === true);
  await page.refresh();
  check("移除后 shicha 不在服务列表", !page.servicesById()["shicha"]);
  check("移除后 registry 不含 shicha", !JSON.stringify(page.regGet()).includes('"services": ["shicha"'));

  const removed = await (await realFetch(`${BASE}/api/removed`, {headers: {"X-LocalServices-Token": token}})).json();
  check("removed 列表含 shicha", (removed.services || []).some(s => s.sid === "shicha"), JSON.stringify(removed).slice(0, 200));

  console.log(out.join("\n"));
  const fails = out.filter(l => l.startsWith("FAIL")).length;
  console.log(`\n==== ${out.length - fails} passed, ${fails} failed ====`);
  process.exit(fails ? 1 : 0);
}
main().catch(e => { console.error(e); process.exit(2); });
