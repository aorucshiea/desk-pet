"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const RENDERER = path.join(__dirname, "..", "src", "pet-chat-renderer.js");

// The new tempo design: the gateway owns the tempo state and emits
// {"event":"tempo","mode":...} every round; the renderer samples the next
// life-loop delay from that mode. Only these three modes exist. Default
// (no tempo event yet) is the slow tier: 300000ms for every body.
const RANGES = {
  ultra: [50000, 70000],
  fast: [90000, 150000],
  slow: [240000, 360000],
};

function element(id) {
  const classes = new Set();
  return {
    id, isConnected: true, value: "", textContent: "", innerHTML: "",
    style: {}, children: [], scrollTop: 0, scrollHeight: 200,
    classList: {
      add: (...names) => names.forEach((n) => classes.add(n)),
      remove: (...names) => names.forEach((n) => classes.delete(n)),
      contains: (n) => classes.has(n),
    },
    addEventListener() {}, appendChild(child) { this.children.push(child); },
    setAttribute() {}, focus() {}, blur() {},
    getBoundingClientRect() { return { width: 320, height: 160, left: 0, top: 0 }; },
  };
}

function stream() {
  const queue = [];
  const waiting = [];
  let done = false;
  function push() {
    while (queue.length && waiting.length) waiting.shift().resolve(queue.shift());
  }
  return {
    event(obj) {
      queue.push({ value: new TextEncoder().encode("data: " + JSON.stringify(obj) + "\n\n") });
      push();
    },
    end() { queue.push({ done: true }); push(); },
    getReader() {
      return {
        read: () => queue.length ? Promise.resolve(queue.shift()) : done
          ? Promise.resolve({ done: true })
          : new Promise((resolve, reject) => waiting.push({ resolve, reject })),
        cancel: () => { done = true; for (const p of waiting.splice(0)) p.resolve({ done: true }); },
      };
    },
  };
}

function baseSandbox() {
  const sandbox = {
    console, TextDecoder, TextEncoder, AbortController, URLSearchParams, Headers,
    // `?boot=1` selects the silent startup path (bootOpen): the standalone
    // path (cmdOpen) invalidateRound()s at unpredictable async points and
    // would race the life round under test — same reason the ownership
    // harness boots silently.
    setTimeout, clearTimeout, setInterval, clearInterval,
    location: { search: "?boot=1" }, requestAnimationFrame: (fn) => fn(),
    document: {
      body: element("body"), activeElement: null,
      documentElement: { setAttribute() {} }, title: "",
      getElementById: () => element("x"), createElement: () => element("created"),
    },
    fetch: async () => ({ ok: false, status: 404, json: async () => ({}), text: async () => "" }),
  };
  sandbox.window = sandbox;
  sandbox.addEventListener = () => {};
  sandbox.getComputedStyle = () => ({ height: "160px", lineHeight: "20px" });
  sandbox.pet = new Proxy({}, { get: () => async () => ({}) });
  return sandbox;
}

// Full harness with a fake /api/chat SSE endpoint, mirroring the
// life-round-ownership.test.js setup (defaultProvider local so the
// physical floor is the local one).
async function setup() {
  const nodes = { bubble: element("bubble"), content: element("content"), updPill: element("updPill") };
  const pending = [];
  const bodies = [];
  const sandbox = baseSandbox();
  sandbox.fetch = async (url, opts) => {
    if (url.includes("/api/chat")) {
      const response = stream();
      pending.push(response);
      try { bodies.push(JSON.parse(opts.body)); } catch {}
      return { ok: true, body: response };
    }
    return { ok: false, status: 404, json: async () => ({}), text: async () => "" };
  };
  sandbox.pet = new Proxy({
    start: async () => ({ ok: true, url: "http://127.0.0.1:18765" }),
    getActiveThemeId: async () => ({ ok: true, themeId: "default" }),
    getProviderPrefs: async () => ({ defaultProvider: "local", modelProviders: [] }),
    getChatParams: async () => ({}), getMemory: async () => ({}),
    saveHistory: async () => {}, loadHistory: async () => null,
  }, { get(target, key) { return key in target ? target[key] : async () => ({}); } });
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(RENDERER, "utf8"), sandbox);
  vm.runInContext("booted=true; sidecarUrl='http://127.0.0.1:18765'; _bootWakeDone=true; _bootWakeScheduled=true;", sandbox);
  return { ctx: sandbox, pending, bodies };
}

// Synchronous variant for pure-function checks: the module loads with
// inert timers, so nothing schedules itself.
function setupSync() {
  const sandbox = baseSandbox();
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(RENDERER, "utf8"), sandbox);
  return { ctx: sandbox };
}

// Capture the round object handed to finishRound (a local of submit(), not
// reachable from the global scope otherwise).
function captureFinishRound(ctx) {
  vm.runInContext(`
    window.__lastRound = null;
    const __origFinishRound = finishRound;
    finishRound = function (round) {
      window.__lastRound = round;
      return __origFinishRound(round);
    };
  `, ctx);
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
async function waitRequest(pending, index) {
  for (let i = 0; i < 500 && pending.length <= index; i++) await tick();
  assert.ok(pending[index], `chat request ${index} was not created`);
}

test("TEMPO_RANGES is exactly the new three-mode set in milliseconds", () => {
  const code = fs.readFileSync(RENDERER, "utf8");
  const start = code.indexOf("const TEMPO_RANGES = {");
  const end = code.indexOf("};", start);
  assert.ok(start !== -1 && end > start, "TEMPO_RANGES literal must exist");
  const chunk = code.slice(start, end);
  assert.doesNotMatch(chunk, /\bstandard\b|\bdaze\b|\basleep\b/, "old modes must be gone");
  const entries = [...chunk.matchAll(/^\s*(\w+):\s*\[(\d+),\s*(\d+)\]/gm)];
  assert.equal(entries.length, 3, "exactly three modes: ultra, fast, slow");
  for (const [, mode, min, max] of entries) {
    assert.deepEqual([Number(min), Number(max)], RANGES[mode], `range of ${mode}`);
  }
});

test("sampleTempoDelay stays inside its mode's bounds and covers both halves", async (t) => {
  const { ctx } = await setup();
  t.after(() => vm.runInContext("_lifeEnabled=false; cancelLifeLoop();", ctx));
  for (const [mode, [min, max]] of Object.entries(RANGES)) {
    let sawBelowMid = false;
    let sawAboveMid = false;
    const mid = (min + max) / 2;
    for (let i = 0; i < 4000; i++) {
      const d = ctx.sampleTempoDelay(mode);
      assert.ok(Number.isInteger(d), "sampled delay is an integer");
      assert.ok(d >= min && d <= max, `${mode}: ${d} must be within [${min}, ${max}]`);
      if (d < mid) sawBelowMid = true;
      if (d > mid) sawAboveMid = true;
    }
    assert.ok(sawBelowMid && sawAboveMid, `${mode}: samples must cover both halves of the range`);
  }
});

test("the tempo SSE event sets round.tempo and the follow-up delay lands in the mode's range", async (t) => {
  for (const [mode, [min, max]] of Object.entries(RANGES)) {
    const { ctx, pending } = await setup();
    t.after(() => vm.runInContext("_lifeEnabled=false; cancelLifeLoop(); invalidateRound();", ctx));
    captureFinishRound(ctx);
    const round = ctx.submitLifeRound();
    await waitRequest(pending, 0);
    pending[0].event({ event: "tempo", mode });
    pending[0].end();
    await round;
    const last = ctx.__lastRound;
    assert.ok(last, "finishRound must have captured the round");
    assert.equal(last.tempo, mode, `mode ${mode} must be accepted into round.tempo`);
    const timer = vm.runInContext("_lifeTimer", ctx);
    assert.ok(timer, "a follow-up life tick must be scheduled");
    const delay = timer._idleTimeout;
    assert.ok(delay >= min && delay <= max,
      `${mode}: scheduled delay ${delay} must be within [${min}, ${max}]`);
  }
});

test("an unknown/removed tempo mode is rejected and the default heartbeat is the slow tier", async (t) => {
  const { ctx, pending } = await setup();
  t.after(() => vm.runInContext("_lifeEnabled=false; cancelLifeLoop(); invalidateRound();", ctx));
  captureFinishRound(ctx);
  const round = ctx.submitLifeRound();
  await waitRequest(pending, 0);
  pending[0].event({ event: "tempo", mode: "standard" });
  pending[0].end();
  await round;
  const last = ctx.__lastRound;
  assert.ok(last, "finishRound must have captured the round");
  assert.equal(last.tempo, null, "removed/unknown modes must not be stored");
  const timer = vm.runInContext("_lifeTimer", ctx);
  assert.ok(timer, "a follow-up life tick must be scheduled");
  assert.equal(timer._idleTimeout, 300000, "no-tempo fallback = slow tier (300000ms)");
});

test("TOOL_STATUS_LINE_RE matches old raw-id and new Chinese-label status lines only", () => {
  const { ctx } = setupSync();
  const re = vm.runInContext("TOOL_STATUS_LINE_RE", ctx);
  const positive = [
    "_执行 capture_screen..._",  // old raw tool id
    "_执行 recall..._",          // old raw tool id, no ellipsis
    "_执行 看屏幕..._",          // new Chinese label (see screen)
    "_执行 深思考..._",          // new Chinese label (deep think)
    "_执行 记忆..._",            // new Chinese label (memory)
    "_执行 走向刚体..._",        // new Chinese label (walk to rigid body)
    "_执行 调节奏_",             // new Chinese label, no ellipsis
  ];
  for (const line of positive) {
    assert.ok(re.test(line), `must match: ${line}`);
  }
  const negative = [
    "我正在执行任务",   // 执行 mid-text in a normal sentence
    "我会执行它。",     // trailing punctuation breaks the anchor
    "请执行 操作",      // instruction sentence starting with 请
    "",                // empty
  ];
  for (const line of negative) {
    assert.ok(!re.test(line), `must NOT match: ${line}`);
  }
});

test("isToolStatusOnlyReply accepts legacy and Chinese-label status-only replies", () => {
  const { ctx } = setupSync();
  assert.equal(ctx.isToolStatusOnlyReply("_执行 capture_screen..._"), true, "old style");
  assert.equal(ctx.isToolStatusOnlyReply("_执行 看屏幕..._"), true, "new style");
  assert.equal(ctx.isToolStatusOnlyReply("_执行 看屏幕..._\n_执行 记忆..._\n_✓ 已完成_"), true,
    "status + result lines only");
  assert.equal(ctx.isToolStatusOnlyReply("我正在执行任务"), false, "normal sentence");
  assert.equal(ctx.isToolStatusOnlyReply("_执行 看屏幕..._\n这是真正的回答"), false, "mixed reply");
  assert.equal(ctx.isToolStatusOnlyReply(""), false, "empty is not a status-only reply");
});

test("stripToolStatusLines keeps real text in mixed replies, old and new style", () => {
  const { ctx } = setupSync();
  assert.equal(
    ctx.stripToolStatusLines("_执行 看屏幕..._\n桌面上有一个蓝色文件夹。"),
    "桌面上有一个蓝色文件夹。",
    "new-style status line stripped",
  );
  assert.equal(
    ctx.stripToolStatusLines("_执行 capture_screen..._\n桌面上有一个蓝色文件夹。"),
    "桌面上有一个蓝色文件夹。",
    "old-style status line stripped",
  );
  assert.equal(
    ctx.stripToolStatusLines("我正在执行任务"),
    "我正在执行任务",
    "normal sentence mentioning 执行 is preserved",
  );
  assert.equal(
    ctx.stripToolStatusLines("_执行 看屏幕..._\n\n\n第一行\n_执行 记忆..._\n\n第二行"),
    "第一行\n\n第二行",
    "status lines removed and blank runs collapsed",
  );
});
