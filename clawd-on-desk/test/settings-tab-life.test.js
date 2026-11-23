"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("node:vm");

const SRC_DIR = path.join(__dirname, "..", "src");
const TAB_SRC = path.join(SRC_DIR, "settings-tab-life.js");
const RENDERER_SRC = path.join(SRC_DIR, "settings-renderer.js");
const HTML_SRC = path.join(SRC_DIR, "settings.html");
const CSS_SRC = path.join(SRC_DIR, "settings.css");

// settings-i18n.js is a plain IIFE ending on `})(globalThis)` — requiring it
// populates globalThis.ClawdSettingsI18n for both the harness and the i18n test.
require("../src/settings-i18n");

// ── Settings → 生命体征 (vitals) card ──
// Display-only card polling the gateway's /api/health (via the existing
// pet-settings:get-status IPC so the x-pet-token handling stays in the
// main process). Locks: bar thresholds, forced-off/died labels, sleep
// states, ledger rows, 5s polling lifecycle, graceful degradation for
// older gateways, and the i18n keys in every locale.

// ── harness: minimal DOM + settings core ───────────────────────────────

// Base class injected into the vm context as `Node` so the tab's
// `c instanceof Node` check passes for text nodes AND elements.
class FakeNodeBase {}

class FakeElement extends FakeNodeBase {
  constructor(tag) {
    super();
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.attributes = {};
    this.className = "";
    this._textContent = "";
    this.style = {};
    this.eventListeners = {};
    this.parentNode = null;
    this.text = null; // set on #text nodes
  }

  // Real DOM semantics: assigning textContent replaces every child.
  get textContent() { return this._textContent; }
  set textContent(value) {
    this._textContent = String(value);
    this.children = [];
  }

  appendChild(child) {
    if (child == null) return child;
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "class") this.className = String(value);
  }

  addEventListener(type, cb) {
    (this.eventListeners[type] = this.eventListeners[type] || []).push(cb);
  }

  removeEventListener(type, cb) {
    const list = this.eventListeners[type] || [];
    const idx = list.indexOf(cb);
    if (idx >= 0) list.splice(idx, 1);
  }

  dispatch(type) {
    for (const cb of (this.eventListeners[type] || []).slice()) cb({});
  }
}

function textOf(node) {
  if (!node) return "";
  let s = node.text != null ? node.text : (node.textContent || "");
  for (const child of node.children || []) s += textOf(child);
  return s;
}

function walk(root, out) {
  out.push(root);
  for (const child of root.children || []) walk(child, out);
  return out;
}

function findByClass(root, cls) {
  return walk(root, []).filter((n) =>
    String(n.className || "").split(/\s+/).includes(cls));
}

function firstByClass(root, cls) {
  return findByClass(root, cls)[0] || null;
}

function makeHarness({ health, zh } = {}) {
  const intervals = [];
  const cleared = [];
  const getStatusCalls = [];

  function createElement(tag) { return new FakeElement(tag); }
  function createTextNode(text) {
    const node = new FakeNodeBase();
    node.text = String(text);
    node.children = [];
    return node;
  }
  const documentShim = {
    visibilityState: "visible",
    eventListeners: {},
    createElement,
    createTextNode,
    addEventListener(type, cb) {
      (this.eventListeners[type] = this.eventListeners[type] || []).push(cb);
    },
    removeEventListener(type, cb) {
      const list = this.eventListeners[type] || [];
      const idx = list.indexOf(cb);
      if (idx >= 0) list.splice(idx, 1);
    },
  };

  const ctx = {
    console,
    Node: FakeNodeBase,
    document: documentShim,
    setInterval: (cb, ms) => { intervals.push({ cb, ms }); return intervals.length; },
    clearInterval: (id) => { cleared.push(id); },
  };
  ctx.globalThis = ctx;
  ctx.window = ctx;
  ctx.petSettings = {
    getStatus: () => {
      getStatusCalls.push(1);
      return Promise.resolve({ health: health === undefined ? null : health });
    },
  };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(TAB_SRC, "utf8"), ctx);

  const STRINGS = globalThis.ClawdSettingsI18n.STRINGS;
  const lang = zh ? "zh" : "en";
  const core = {
    helpers: { t: (key) => (STRINGS[lang] && STRINGS[lang][key]) || (STRINGS.en && STRINGS.en[key]) || key },
    tabs: {},
  };
  ctx.ClawdSettingsTabLife.init(core);

  const parent = new FakeElement("div");
  return {
    ctx, core, parent, intervals, cleared, getStatusCalls, documentShim,
    tab: core.tabs.life,
    render: () => { ctx.ClawdSettingsTabLife.init(core); core.tabs.life.render(parent); },
    settle: () => new Promise((resolve) => setTimeout(resolve, 0)),
  };
}

const HEALTH = {
  ok: true,
  vitals: {
    battery_percent: 87.5,
    forced_off: false,
    should_warn: false,
    hp: 100.0,
    died: false,
    asleep: false,
    sleep_stage: null,
  },
  ledger: {
    round_count: 1234,
    counts: { fast: 40, slow: 12, organize: 2 },
    pending_recap: false,
  },
};

// ── script integrity / registration ───────────────────────────────────

test("settings-tab-life.js loads via the sibling IIFE pattern and registers the tab", () => {
  const code = fs.readFileSync(TAB_SRC, "utf8");
  assert.match(code, /root\.ClawdSettingsTabLife\s*=\s*\{\s*init\s*\}/);
  assert.match(code, /core\.tabs\.life\s*=/);
  assert.match(code, /onExit/);
});

test("settings-tab-life.js stays browser-script friendly (no CommonJS, no alert)", () => {
  const code = fs.readFileSync(TAB_SRC, "utf8");
  assert.ok(!code.includes("require("), "must not use require()");
  assert.ok(!code.includes("module.exports"), "must not use module.exports");
  assert.doesNotMatch(code, /\balert\(/, "alert() blocks the main process in Electron");
});

test("the tab never touches the gateway itself — token handling stays main-process", () => {
  const code = fs.readFileSync(TAB_SRC, "utf8");
  // The settings window's CSP has no connect-src; the card must go through
  // window.petSettings.getStatus() (settings-ipc sidecar proxy) instead.
  assert.ok(!code.includes("fetch("), "no direct fetch — CSP forbids connect-src");
  assert.ok(!code.includes("x-pet-token"), "token must not appear in renderer code");
  assert.ok(!code.includes("18765"), "no hardcoded gateway port in renderer code");
  assert.match(code, /petSettings\.getStatus\(/);
});

test("settings.html includes settings-tab-life.js after web-model and before settings-renderer.js", () => {
  const html = fs.readFileSync(HTML_SRC, "utf8");
  const lifeIdx = html.indexOf("settings-tab-life.js");
  const webModelIdx = html.indexOf("settings-tab-web-model.js");
  const rendererIdx = html.indexOf("settings-renderer.js");
  assert.ok(lifeIdx > 0, "settings-tab-life.js must appear in settings.html");
  assert.ok(webModelIdx > 0 && webModelIdx < lifeIdx, "should load after settings-tab-web-model.js");
  assert.ok(rendererIdx > lifeIdx, "settings-renderer.js must come after settings-tab-life.js");
});

test("settings-renderer.js registers the life tab (sidebar entry + guarded init)", () => {
  const code = fs.readFileSync(RENDERER_SRC, "utf8");
  assert.match(code, /id:\s*"life"/);
  assert.match(code, /labelKey:\s*"sidebarLife"/);
  assert.match(code, /if \(globalThis\.ClawdSettingsTabLife\) globalThis\.ClawdSettingsTabLife\.init\(core\);/);
});

test("settings.css carries the vitals card styles incl. level classes", () => {
  const css = fs.readFileSync(CSS_SRC, "utf8");
  for (const cls of [".life-card", ".life-vital-row", ".life-bar-fill", ".life-pill", ".life-ledger-row", ".life-note", ".life-footnote"]) {
    assert.ok(css.includes(cls), `settings.css must define ${cls}`);
  }
  for (const lvl of ["level-normal", "level-warn", "level-danger"]) {
    assert.ok(css.includes(`.life-bar-fill.${lvl}`), `settings.css must define ${lvl} bar tone`);
  }
});

// ── rendering from a mocked health payload ────────────────────────────

test("renders battery/hp bars, sleep line and ledger rows from a health payload", async () => {
  const h = makeHarness({ health: HEALTH, zh: true });
  h.render();
  await h.settle();

  const rows = findByClass(h.parent, "life-vital-row");
  assert.equal(rows.length, 3, "battery + hp + sleep rows (no tempo row without a tempo field)");

  const battery = firstByClass(h.parent, "life-bar-fill");
  assert.equal(battery.style.width, "87.5%");
  assert.ok(battery.className.includes("level-normal"), "87.5% battery is normal");
  const values = findByClass(h.parent, "life-vital-value");
  assert.equal(textOf(values[0]), "87.5%");
  assert.equal(textOf(values[1]), "100 / 100");

  assert.ok(!findByClass(h.parent, "life-pill").length, "awake → no 睡眠中 pill");
  assert.equal(textOf(values[2]), "清醒");

  const ledgerRows = findByClass(h.parent, "life-ledger-row");
  assert.equal(ledgerRows.length, 4, "rounds + fast + slow + organize");
  assert.deepEqual(ledgerRows.map((r) => textOf(r.children[r.children.length - 1])), ["1234", "40", "12", "2"]);
  assert.ok(!findByClass(h.parent, "life-note").length, "no pending-recap note");

  const footnote = firstByClass(h.parent, "life-footnote");
  assert.equal(textOf(footnote), "数据每 5 秒刷新 · 睡眠时长由凌凌自己决定；被吵醒会让它头痛");
});

test("manual refresh button re-polls getStatus", async () => {
  const h = makeHarness({ health: HEALTH, zh: true });
  h.render();
  await h.settle();
  const before = h.getStatusCalls.length;
  const btn = findByClass(h.parent, "soft-btn")[0];
  assert.ok(btn, "refresh button exists");
  btn.dispatch("click");
  await h.settle();
  assert.ok(h.getStatusCalls.length > before, "click triggers a refresh");
});

// ── threshold classes ──────────────────────────────────────────────────

async function barTonesFor(batteryPercent, hp) {
  const h = makeHarness({
    zh: true,
    health: {
      ...HEALTH,
      vitals: { ...HEALTH.vitals, battery_percent: batteryPercent, hp, forced_off: false, died: false },
    },
  });
  h.render();
  await h.settle();
  const fills = findByClass(h.parent, "life-bar-fill");
  return { battery: fills[0].className, hp: fills[1].className };
}

test("battery bar classes flip at the 50/25 thresholds", async () => {
  assert.ok((await barTonesFor(51, 100)).battery.includes("level-normal"));
  assert.ok((await barTonesFor(50, 100)).battery.includes("level-warn"));
  assert.ok((await barTonesFor(25, 100)).battery.includes("level-warn"));
  assert.ok((await barTonesFor(24.9, 100)).battery.includes("level-danger"));
  assert.ok((await barTonesFor(0, 100)).battery.includes("level-danger"));
});

test("hp bar classes flip at the 50/20 thresholds", async () => {
  assert.ok((await barTonesFor(100, 51)).hp.includes("level-normal"));
  assert.ok((await barTonesFor(100, 50)).hp.includes("level-warn"));
  assert.ok((await barTonesFor(100, 20)).hp.includes("level-warn"));
  assert.ok((await barTonesFor(100, 19.9)).hp.includes("level-danger"));
  assert.ok((await barTonesFor(100, 0)).hp.includes("level-danger"));
});

// ── forced_off / died / sleep states ──────────────────────────────────

test("forced_off shows the charging label instead of a percent and freezes the bar", async () => {
  const h = makeHarness({
    zh: true,
    health: { ...HEALTH, vitals: { ...HEALTH.vitals, battery_percent: 18, forced_off: true } },
  });
  h.render();
  await h.settle();
  const values = findByClass(h.parent, "life-vital-value");
  assert.equal(textOf(values[0]), "已强制停机充电中（回到 30% 恢复）");
  const fill = firstByClass(h.parent, "life-bar-fill");
  assert.ok(fill.className.includes("level-danger"));
  assert.ok(fill.className.includes("is-static"), "no bar animation while forced off");
  assert.equal(fill.style.width, "18%");
});

test("died shows the graveyard label with an empty hp bar", async () => {
  const h = makeHarness({
    zh: true,
    health: { ...HEALTH, vitals: { ...HEALTH.vitals, hp: 0, died: true } },
  });
  h.render();
  await h.settle();
  const values = findByClass(h.parent, "life-vital-value");
  assert.equal(textOf(values[1]), "记忆已归于墓地");
  const fills = findByClass(h.parent, "life-bar-fill");
  assert.ok(fills[1].className.includes("level-danger"));
  assert.equal(fills[1].style.width, "0%");
});

test("sleep: pill + stage label, remaining minutes from sleep_remaining_s", async () => {
  const h = makeHarness({
    zh: true,
    health: {
      ...HEALTH,
      vitals: { ...HEALTH.vitals, asleep: true, sleep_stage: 2, sleep_remaining_s: 5400 },
    },
  });
  h.render();
  await h.settle();
  const pill = firstByClass(h.parent, "life-pill");
  assert.ok(pill, "asleep → 睡眠中 pill");
  assert.equal(textOf(pill), "睡眠中");
  const values = findByClass(h.parent, "life-vital-value");
  assert.equal(textOf(values[2]), "睡眠中（第 2 阶被吵醒·剩余 90 分钟）");
});

test("sleep: missing sleep_stage falls back to stage 1; forced sleep has its own label", async () => {
  const h1 = makeHarness({
    zh: true,
    health: { ...HEALTH, vitals: { ...HEALTH.vitals, asleep: true, sleep_stage: null } },
  });
  h1.render();
  await h1.settle();
  const values1 = findByClass(h1.parent, "life-vital-value");
  assert.equal(textOf(values1[2]), "睡眠中（第 1 阶被吵醒）");

  const h2 = makeHarness({
    zh: true,
    health: {
      ...HEALTH,
      vitals: { ...HEALTH.vitals, asleep: true, sleep_stage: 1, sleep_forced: true, sleep_remaining_min: 480 },
    },
  });
  h2.render();
  await h2.settle();
  const values2 = findByClass(h2.parent, "life-vital-value");
  assert.equal(textOf(values2[2]), "强制休眠（剩余 480 分钟）");
});

test("tempo row renders when the payload carries a mode and stays hidden otherwise", async () => {
  const without = makeHarness({ zh: true, health: HEALTH });
  without.render();
  await without.settle();
  assert.equal(findByClass(without.parent, "life-vital-row").length, 3, "no tempo row yet");

  const withTempo = makeHarness({
    zh: true,
    health: { ...HEALTH, vitals: { ...HEALTH.vitals, tempo_mode: "fast" } },
  });
  withTempo.render();
  await withTempo.settle();
  const rows = findByClass(withTempo.parent, "life-vital-row");
  assert.equal(rows.length, 4);
  const tempoValue = findByClass(rows[3], "life-vital-value")[0];
  assert.equal(textOf(tempoValue), "快反应");
});

test("pending_recap adds the hourly-recap note", async () => {
  const h = makeHarness({
    zh: true,
    health: { ...HEALTH, ledger: { ...HEALTH.ledger, pending_recap: true } },
  });
  h.render();
  await h.settle();
  const note = firstByClass(h.parent, "life-note");
  assert.equal(textOf(note), "有一条小时总结待回想");
  assert.ok(note.className.includes("warn"));
});

// ── graceful degradation ──────────────────────────────────────────────

test("unreachable gateway shows the offline note", async () => {
  const h = makeHarness({ health: null, zh: true });
  h.render();
  await h.settle();
  const note = firstByClass(h.parent, "life-note");
  assert.equal(textOf(note), "无法连接网关——桌宠在运行吗？");
  assert.ok(note.className.includes("danger"));
  assert.equal(findByClass(h.parent, "life-bar-fill").length, 0, "no bars without data");
});

test("older gateway (no vitals object) asks for a restart", async () => {
  const h = makeHarness({ health: { ok: true }, zh: true });
  h.render();
  await h.settle();
  const note = firstByClass(h.parent, "life-note");
  assert.equal(textOf(note), "需要重启网关以启用生命体征。");
  assert.ok(note.className.includes("warn"));
});

test("vitals without a ledger object keeps the bars and shows placeholder ledger values", async () => {
  const h = makeHarness({ health: { ok: true, vitals: HEALTH.vitals }, zh: true });
  h.render();
  await h.settle();
  assert.equal(findByClass(h.parent, "life-bar-fill").length, 2, "bars still render");
  const ledgerRows = findByClass(h.parent, "life-ledger-row");
  assert.equal(ledgerRows.length, 4);
  assert.ok(ledgerRows.every((r) => textOf(r.children[r.children.length - 1]) === "—"));
});

test("missing vitals fields degrade per-row (no battery field → 0% bar, no crash)", async () => {
  const h = makeHarness({ health: { ok: true, vitals: {} }, zh: true });
  h.render();
  await h.settle();
  const values = findByClass(h.parent, "life-vital-value");
  assert.equal(textOf(values[0]), "0%");
  assert.equal(textOf(values[1]), "0 / 100");
  assert.equal(textOf(values[2]), "清醒");
});

// ── polling lifecycle ─────────────────────────────────────────────────

test("polls every 5s, pauses while hidden, resumes + refreshes on visibilitychange, stops on exit", async () => {
  const h = makeHarness({ health: HEALTH, zh: true });
  h.render();
  await h.settle();

  assert.equal(h.intervals.length, 1, "one polling interval");
  assert.equal(h.intervals[0].ms, 5000, "5 second poll");
  const initial = h.getStatusCalls.length;
  assert.ok(initial >= 1, "initial paint pulled data");

  h.documentShim.visibilityState = "hidden";
  h.intervals[0].cb();
  await h.settle();
  assert.equal(h.getStatusCalls.length, initial, "no refresh while hidden");

  h.documentShim.visibilityState = "visible";
  h.intervals[0].cb();
  await h.settle();
  assert.ok(h.getStatusCalls.length > initial, "refresh resumes when visible");

  const visibleHandlers = h.documentShim.eventListeners.visibilitychange || [];
  assert.ok(visibleHandlers.length === 1, "one visibilitychange listener");

  h.tab.onExit();
  assert.ok(h.cleared.length >= 1, "onExit clears the interval");
  const after = h.getStatusCalls.length;
  h.documentShim.visibilityState = "visible";
  for (const cb of visibleHandlers) cb({});
  await h.settle();
  assert.equal(h.getStatusCalls.length, after, "no refresh after exit");
});

test("overlapping refreshes are collapsed (in-flight guard)", async () => {
  let resolveStatus;
  const h = makeHarness({ health: undefined, zh: true });
  const countingGetStatus = h.ctx.petSettings.getStatus;
  h.ctx.petSettings.getStatus = () => {
    h.getStatusCalls.push(1);
    return new Promise((resolve) => { resolveStatus = resolve; });
  };
  h.render();
  await h.settle();
  void countingGetStatus;
  assert.equal(h.getStatusCalls.length, 1, "single request while the first is in flight");
  h.intervals[0].cb();
  await h.settle();
  assert.equal(h.getStatusCalls.length, 1, "tick during in-flight refresh is dropped");
  resolveStatus({ health: HEALTH });
  await h.settle();
  const values = findByClass(h.parent, "life-vital-value");
  assert.equal(textOf(values[0]), "87.5%", "late paint still lands");
});

// ── i18n: keys exist in all locales, with real translations ───────────

test("settings-i18n.js: all language packs include the vitals keys", () => {
  const KEYS = [
    "sidebarLife",
    "lifeTitle",
    "lifeSubtitle",
    "lifeRefresh",
    "lifeOffline",
    "lifeNeedRestart",
    "lifeBattery",
    "lifeBatteryForcedOff",
    "lifeHp",
    "lifeDied",
    "lifeSleep",
    "lifeSleepPill",
    "lifeSleepAwake",
    "lifeSleepAsleep",
    "lifeSleepAsleepRemaining",
    "lifeSleepForced",
    "lifeSleepForcedRemaining",
    "lifeTempo",
    "lifeTempoUltra",
    "lifeTempoFast",
    "lifeTempoSlow",
    "lifeLedger",
    "lifeLedgerRounds",
    "lifeLedgerFast",
    "lifeLedgerSlow",
    "lifeLedgerOrganize",
    "lifeLedgerPending",
    "lifeFooterNote",
  ];
  const STRINGS = globalThis.ClawdSettingsI18n.STRINGS;
  for (const key of KEYS) {
    for (const lang of ["en", "zh", "zh-TW", "ko", "ja"]) {
      const value = STRINGS[lang] && STRINGS[lang][key];
      assert.ok(typeof value === "string" && value.length > 0, `${lang}.${key} is missing`);
    }
  }
  // CJK locales must carry real translations, not the English sentence.
  for (const lang of ["zh", "zh-TW", "ko", "ja"]) {
    for (const key of ["lifeTitle", "lifeBattery", "lifeHp", "lifeDied", "lifeFooterNote", "lifeNeedRestart"]) {
      assert.notStrictEqual(STRINGS[lang][key], STRINGS.en[key], `${lang}.${key} is still the English sentence`);
    }
  }
});
