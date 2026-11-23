"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

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

async function setup() {
  const nodes = { bubble: element("bubble"), content: element("content"), updPill: element("updPill") };
  const pending = [];
  const bodies = [];
  const ctx = {
    console, TextDecoder, TextEncoder, AbortController, URLSearchParams, Headers,
    setTimeout, clearTimeout, setInterval, clearInterval,
    location: { search: "?boot=1" }, requestAnimationFrame: (fn) => fn(),
    document: {
      body: element("body"), activeElement: null,
      documentElement: { setAttribute() {} }, title: "",
      getElementById: (id) => nodes[id] || (nodes[id] = element(id)),
      createElement: () => element("created"),
    },
    fetch: async (url, opts) => {
          if (url.includes("/api/chat")) {
        const response = stream();
        pending.push(response);
        try { bodies.push(JSON.parse(opts.body)); } catch {}
        return { ok: true, body: response };
      }
      return { ok: false, status: 404, json: async () => ({}), text: async () => "" };
    },
  };
  ctx.window = ctx;
  ctx.addEventListener = () => {};
  ctx.getComputedStyle = () => ({ height: "160px", lineHeight: "20px" });
  ctx.pet = new Proxy({
    start: async () => ({ ok: true, url: "http://127.0.0.1:18765" }),
    getActiveThemeId: async () => ({ ok: true, themeId: "default" }),
    getProviderPrefs: async () => ({ defaultProvider: "deepseek-web", modelProviders: [] }),
    getChatParams: async () => ({}), getMemory: async () => ({}),
    saveHistory: async () => {}, loadHistory: async () => null,
  }, { get(target, key) { return key in target ? target[key] : async () => ({}); } });
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "src", "pet-chat-renderer.js"), "utf8"), ctx);
  vm.runInContext("booted=true; sidecarUrl='http://127.0.0.1:18765'; _bootWakeDone=true; _bootWakeScheduled=true;", ctx);
  return { ctx, pending, bodies, nodes };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
async function waitRequest(pending, index) {
  for (let i = 0; i < 500 && pending.length <= index; i++) await tick();
  assert.ok(pending[index], `chat request ${index} was not created`);
}

test("a life turn owns the request before async setup; a second cannot overlap", async (t) => {
  const { ctx, pending } = await setup();
  t.after(() => vm.runInContext("_bootWakeDone=true; _lifeEnabled=false; cancelLifeLoop(); invalidateRound();", ctx));
  const first = ctx.submitLifeRound();
  const second = ctx.submitLifeRound();
  await waitRequest(pending, 0);
  assert.equal(pending.length, 1);
  pending[0].event({ event: "inner" });
  pending[0].end();
  await Promise.all([first, second]);
  assert.equal(ctx.roundBusy(), false);
  assert.equal(ctx.curHistory().length, 0);
});

test("empty input focus does not stop life; quiet turns do not replace human dialogue", async (t) => {
  const { ctx, pending } = await setup();
  t.after(() => vm.runInContext("_bootWakeDone=true; _lifeEnabled=false; cancelLifeLoop(); invalidateRound();", ctx));
  await ctx.showAsk("earlier human answer");
  ctx.curHistory().push({ role: "user", content: "human question" });
  ctx.curHistory().push({ role: "assistant", content: "human answer" });
  ctx.document.activeElement = vm.runInContext("inputEl", ctx);
  for (let i = 0; i < 5; i++) {
    const round = ctx.submitLifeRound();
    await waitRequest(pending, i);
    assert.equal(pending.length, i + 1);
    pending[i].event({ event: "inner" });
    pending[i].end();
    await round;
  }
  assert.deepEqual([...ctx.curHistory()].map((m) => m.content), ["human question", "human answer"]);
});

test("a draft survives automatic visible output and the automatic prompt is not persisted", async (t) => {
  const { ctx, pending } = await setup();
  t.after(() => vm.runInContext("_bootWakeDone=true; _lifeEnabled=false; cancelLifeLoop(); invalidateRound();", ctx));
  await ctx.showAsk("previous answer");
  const draftEl = vm.runInContext("inputEl", ctx);
  draftEl.value = "unfinished human draft";
  ctx.document.activeElement = draftEl;
  const round = ctx.submitLifeRound();
  await waitRequest(pending, 0);
  pending[0].event({ event: "delta", content: "life observation" });
  pending[0].end();
  await round;
  assert.equal(vm.runInContext("inputEl", ctx).value, "unfinished human draft");
  assert.equal(ctx.roundBusy(), false);
  assert.equal(ctx.curHistory().length, 0);
});

test("tool progress followed by INNER releases the turn instead of leaving a busy bubble", async (t) => {
  const { ctx, pending } = await setup();
  t.after(() => vm.runInContext("_bootWakeDone=true; _lifeEnabled=false; cancelLifeLoop(); invalidateRound();", ctx));
  const first = ctx.submitLifeRound();
  await waitRequest(pending, 0);
  pending[0].event({ event: "delta", content: "_执行 capture_screen..._" });
  pending[0].event({ event: "inner" });
  pending[0].end();
  await first;
  assert.equal(ctx.roundBusy(), false);
  const next = ctx.submitLifeRound();
  await waitRequest(pending, 1);
  assert.equal(pending.length, 2);
  pending[1].end();
  await next;
});

test("a late response from an interrupted life turn cannot write the new human turn", async (t) => {
  const { ctx, pending } = await setup();
  t.after(() => vm.runInContext("_bootWakeDone=true; _lifeEnabled=false; cancelLifeLoop(); invalidateRound();", ctx));
  void ctx.submitLifeRound();
  await waitRequest(pending, 0);
  const human = ctx.submit("human now");
  await waitRequest(pending, 1);
  pending[0].event({ event: "delta", content: "late autonomous text" });
  pending[0].end();
  pending[1].event({ event: "delta", content: "human reply" });
  pending[1].end();
  await human;
  assert.deepEqual([...ctx.curHistory()].map((m) => m.content), ["human now", "human reply"]);
});

test("the life prompt reaches the model as the last user message", async (t) => {
  const { ctx, pending, bodies } = await setup();
  t.after(() => vm.runInContext("_bootWakeDone=true; _lifeEnabled=false; cancelLifeLoop(); invalidateRound();", ctx));
  const round = ctx.submitLifeRound();
  await waitRequest(pending, 0);
  const msgs = bodies[0].messages;
  const last = msgs[msgs.length - 1];
  assert.equal(last.role, "user");
  assert.ok(last.content.includes("生命在继续"), "LIFE_PROMPT must be in the request");
  pending[0].event({ event: "inner" });
  pending[0].end();
  await round;
  assert.equal(ctx.curHistory().length, 0, "life prompt must not persist in history");
});

test("input stays live while the pet streams and can interrupt", async (t) => {
  const { ctx, pending } = await setup();
  t.after(() => vm.runInContext("_bootWakeDone=true; _lifeEnabled=false; cancelLifeLoop(); invalidateRound();", ctx));
  const human = ctx.submit("first question");
  await waitRequest(pending, 0);
  pending[0].event({ event: "delta", content: "streaming answer" });
  await tick();
  // While streaming, an input must exist and accept a draft.
  const input = vm.runInContext("inputEl", ctx);
  assert.ok(input, "input must exist during speak");
  input.value = "interrupt now";
  const interrupted = ctx.submit("interrupt now");
  await waitRequest(pending, 1);
  pending[0].end();
  pending[1].event({ event: "delta", content: "second answer" });
  pending[1].end();
  await interrupted;
  const contents = [...ctx.curHistory()].map((m) => m.content);
  assert.ok(contents.includes("interrupt now"), "the interrupting user turn must be recorded");
});

test("web body keeps a slower physical floor; the no-tempo default is the slow tier everywhere", async (t) => {
  const { ctx } = await setup();
  t.after(() => vm.runInContext("_bootWakeDone=true; _lifeEnabled=false; cancelLifeLoop(); invalidateRound();", ctx));
  vm.runInContext("_lastProviderName='deepseek-web'", ctx);
  assert.equal(ctx.defaultLifeDelay(), 300000);
  assert.equal(ctx.lifeFloor(), 10000);
  vm.runInContext("_lastProviderName='local'", ctx);
  assert.equal(ctx.defaultLifeDelay(), 300000);
  assert.equal(ctx.lifeFloor(), 300);
});

test("five life rounds do not accumulate assistant replies in history", async (t) => {
  const { ctx, pending, bodies } = await setup();
  t.after(() => vm.runInContext("_bootWakeDone=true; _lifeEnabled=false; cancelLifeLoop(); invalidateRound();", ctx));
  ctx.curHistory().push({ role: "user", content: "real question" });
  ctx.curHistory().push({ role: "assistant", content: "real answer" });
  for (let i = 0; i < 5; i++) {
    const round = ctx.submitLifeRound();
    await waitRequest(pending, i);
    const msgs = bodies[i].messages;
    assert.ok(msgs[msgs.length - 1].content.includes("生命在继续"));
    assert.ok(!msgs.some((m) => m.role === "assistant" && m.content.includes("life reply")),
      "previous life replies must not appear in the next request");
    pending[i].event({ event: "delta", content: "life reply " + i });
    pending[i].end();
    await round;
  }
  assert.deepEqual([...ctx.curHistory()].map((m) => m.content), ["real question", "real answer"]);
});
