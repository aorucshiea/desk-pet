"use strict";

// ── Settings → 生命体征 (vitals) tab ──
// Display-only card for the pet's living state: 电量 (battery), 血量 (HP),
// 睡眠 (sleep) and the 生命台账 (round ledger). It owns NO settings and
// therefore never writes through settings-controller — it only POLLS the
// gateway's /api/health and renders what comes back (same lifecycle shape
// as settings-tab-web-model.js: render → interval → onExit).
//
// Data path: window.petSettings.getStatus() (the settings-ipc sidecar
// proxy, which authenticates in the main process) → { health: <full
// /api/health JSON> }. The settings window's CSP has no connect-src, so
// the renderer never fetches the gateway directly; the main-process
// proxy is the only route.
//
// Older gateways answer /api/health without `vitals`/`ledger` — the card
// degrades to a "需要重启网关以启用生命体征" note instead of blank rows.
// Fields the gateway does not expose yet (sleep remaining minutes, forced
// sleep flag, tempo mode) light up automatically when the payload grows
// them; until then the row shows what is known.

(function initSettingsTabLife(root) {
  var core = null;
  var helpers = null;
  var mounted = false;
  var timer = null;
  var refreshInFlight = false;
  var visibilityHandler = null;
  var refs = null;

  var POLL_MS = 5000;

  // Bar thresholds. Battery: >50 正常, 25–50 警告, <25 危险.
  // HP: >50 正常, 20–50 警告, <20 危险.
  var BATTERY_WARN_AT = 50;
  var BATTERY_DANGER_AT = 25;
  var HP_WARN_AT = 50;
  var HP_DANGER_AT = 20;

  var TEMPO_KEYS = { ultra: "lifeTempoUltra", fast: "lifeTempoFast", slow: "lifeTempoSlow" };

  function t(key) { try { return helpers.t(key); } catch (e) { return key; } }

  // {n}/{stage}-style placeholder fill (same convention as i18n.js).
  function tf(key, map) {
    var s = t(key);
    for (var k in map) {
      if (!Object.prototype.hasOwnProperty.call(map, k)) continue;
      s = s.split("{" + k + "}").join(String(map[k]));
    }
    return s;
  }

  function el(tag, attrs) {
    var children = [], len = arguments.length - 2;
    while (len-- > 0) children[len] = arguments[len + 2];
    try {
      var e = document.createElement(tag);
      if (attrs && typeof attrs === "object" && !Array.isArray(attrs)) {
        for (var k in attrs) {
          if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
          var v = attrs[k];
          if (k === "className") { e.className = v; }
          else if (k === "style" && typeof v === "object") { for (var sk in v) e.style[sk] = v[sk]; }
          else if (k.startsWith("on")) { e.addEventListener(k.slice(2).toLowerCase(), v); }
          else if (v !== undefined && v !== null) { e.setAttribute(k, v); }
        }
      }
      if (children) {
        for (var ci = 0; ci < children.length; ci++) {
          var c = children[ci];
          if (c == null) continue;
          if (typeof c === "string" || typeof c === "number") e.appendChild(document.createTextNode(String(c)));
          else if (c instanceof Node) e.appendChild(c);
          else if (Array.isArray(c)) { for (var cj = 0; cj < c.length; cj++) { if (c[cj] instanceof Node) e.appendChild(c[cj]); } }
        }
      }
      return e;
    } catch (ex) { console.warn("life: el error", ex); return document.createElement("div"); }
  }

  function clear(node) {
    if (!node) return;
    try {
      while (node.firstChild) node.removeChild(node.firstChild);
    } catch (e) { console.warn("life: clear error", e); }
  }

  function clampPct(raw) {
    var n = Number(raw);
    if (!isFinite(n)) return 0;
    return Math.max(0, Math.min(100, n));
  }

  function levelClass(pct, warnAt, dangerAt) {
    if (pct > warnAt) return "level-normal";
    if (pct >= dangerAt) return "level-warn";
    return "level-danger";
  }

  function fmtPct(pct) {
    var s = pct.toFixed(1);
    return s.endsWith(".0") ? s.slice(0, -2) : s;
  }

  function noteEl(text, kind) {
    return el("div", { className: "life-note " + (kind || "info") }, text);
  }

  // One vital row: label (+optional pill) / value / optional bar.
  function vitalRow(labelText, withBar) {
    var row = el("div", { className: "life-vital-row" });
    var head = el("div", { className: "life-vital-head" });
    var labelHost = el("span", { className: "life-vital-labelwrap" });
    labelHost.appendChild(el("span", { className: "life-vital-label" }, labelText));
    var value = el("span", { className: "life-vital-value" }, "—");
    head.appendChild(labelHost);
    head.appendChild(value);
    row.appendChild(head);
    var fill = null;
    if (withBar) {
      var bar = el("div", { className: "life-bar" });
      fill = el("div", { className: "life-bar-fill", style: { width: "0%" } });
      bar.appendChild(fill);
      row.appendChild(bar);
    }
    return { row: row, labelHost: labelHost, value: value, fill: fill };
  }

  function ledgerRow(labelText, valueText) {
    var row = el("div", { className: "life-ledger-row" });
    row.appendChild(el("span", { className: "life-ledger-label" }, labelText));
    row.appendChild(el("span", { className: "life-ledger-value" }, String(valueText)));
    return row;
  }

  function sleepRemainingMin(v) {
    // The gateway does not expose sleep time yet; render it the moment the
    // payload carries either shape (minutes or seconds).
    var mins = v.sleep_remaining_min;
    if (typeof mins === "number" && isFinite(mins) && mins >= 0) return Math.round(mins);
    var secs = v.sleep_remaining_s;
    if (typeof secs === "number" && isFinite(secs) && secs >= 0) return Math.max(1, Math.round(secs / 60));
    return null;
  }

  function tempoLabel(mode) {
    var key = TEMPO_KEYS[mode];
    return key ? t(key) : String(mode);
  }

  function buildBody(health) {
    var body = el("div", { className: "life-body" });

    if (!health || typeof health !== "object") {
      body.appendChild(noteEl(t("lifeOffline"), "danger"));
      return body;
    }
    var v = (health.vitals && typeof health.vitals === "object") ? health.vitals : null;
    if (!v) {
      // Older gateway: /api/health has no vitals object yet.
      body.appendChild(noteEl(t("lifeNeedRestart"), "warn"));
      return body;
    }

    // ── 电量 ──
    var bp = clampPct(v.battery_percent);
    var battery = vitalRow(t("lifeBattery"), true);
    if (v.forced_off === true) {
      // 强制停机：不显示百分比读数，显示充电恢复说明（条保持静止）。
      battery.value.textContent = t("lifeBatteryForcedOff");
      battery.value.className = "life-vital-value life-tone-danger";
      battery.fill.className = "life-bar-fill level-danger is-static";
      battery.fill.style.width = bp + "%";
    } else {
      battery.value.textContent = fmtPct(bp) + "%";
      battery.fill.className = "life-bar-fill " + levelClass(bp, BATTERY_WARN_AT, BATTERY_DANGER_AT);
      battery.fill.style.width = bp + "%";
    }
    body.appendChild(battery.row);

    // ── 血量 ──
    var hp = clampPct(v.hp);
    var hpRow = vitalRow(t("lifeHp"), true);
    if (v.died === true) {
      hpRow.value.textContent = t("lifeDied");
      hpRow.value.className = "life-vital-value life-tone-danger";
      hpRow.fill.className = "life-bar-fill level-danger is-static";
      hpRow.fill.style.width = "0%";
    } else {
      hpRow.value.textContent = fmtPct(hp) + " / 100";
      hpRow.fill.className = "life-bar-fill " + levelClass(hp, HP_WARN_AT, HP_DANGER_AT);
      hpRow.fill.style.width = hp + "%";
    }
    body.appendChild(hpRow.row);

    // ── 睡眠 ──
    var sleepRow = vitalRow(t("lifeSleep"), false);
    if (v.asleep === true) {
      sleepRow.labelHost.appendChild(el("span", { className: "life-pill" }, t("lifeSleepPill")));
      var mins = sleepRemainingMin(v);
      if (v.sleep_forced === true) {
        // Gateway does not expose sleep_forced yet; lights up when it does.
        sleepRow.value.textContent = mins !== null
          ? tf("lifeSleepForcedRemaining", { n: mins })
          : t("lifeSleepForced");
      } else {
        var stage = Number(v.sleep_stage);
        if (!isFinite(stage) || stage < 1) stage = 1;
        sleepRow.value.textContent = mins !== null
          ? tf("lifeSleepAsleepRemaining", { stage: stage, n: mins })
          : tf("lifeSleepAsleep", { stage: stage });
      }
    } else {
      sleepRow.value.textContent = t("lifeSleepAwake");
    }
    body.appendChild(sleepRow.row);

    // ── 节奏 ──
    // /api/health does not carry the tempo mode yet (it rides the per-round
    // tempo SSE event); feature-detect both spots so the row appears as
    // soon as the gateway exposes it.
    var tempoMode = null;
    if (typeof v.tempo_mode === "string" && v.tempo_mode) tempoMode = v.tempo_mode;
    else if (typeof health.tempo_mode === "string" && health.tempo_mode) tempoMode = health.tempo_mode;
    if (tempoMode) {
      var tempoRow = vitalRow(t("lifeTempo"), false);
      tempoRow.value.textContent = tempoLabel(tempoMode);
      body.appendChild(tempoRow.row);
    }

    // ── 生命台账 ──
    var ledger = (health.ledger && typeof health.ledger === "object") ? health.ledger : null;
    var ledgerBox = el("div", { className: "life-ledger" });
    ledgerBox.appendChild(el("div", { className: "life-section-title" }, t("lifeLedger")));
    var counts = (ledger && ledger.counts && typeof ledger.counts === "object") ? ledger.counts : {};
    ledgerBox.appendChild(ledgerRow(t("lifeLedgerRounds"), ledger && ledger.round_count != null ? ledger.round_count : "—"));
    ledgerBox.appendChild(ledgerRow(t("lifeLedgerFast"), counts.fast != null ? counts.fast : "—"));
    ledgerBox.appendChild(ledgerRow(t("lifeLedgerSlow"), counts.slow != null ? counts.slow : "—"));
    ledgerBox.appendChild(ledgerRow(t("lifeLedgerOrganize"), counts.organize != null ? counts.organize : "—"));
    if (ledger && ledger.pending_recap === true) {
      ledgerBox.appendChild(noteEl(t("lifeLedgerPending"), "warn"));
    }
    body.appendChild(ledgerBox);

    return body;
  }

  function paint(health, target) {
    var host = target && target.bodyHost;
    if (!host) return;
    clear(host);
    host.appendChild(buildBody(health));
  }

  async function refresh() {
    if (!mounted || refreshInFlight) return;
    refreshInFlight = true;
    var target = refs;
    try {
      var health = null;
      try {
        if (typeof window !== "undefined" && window.petSettings
          && typeof window.petSettings.getStatus === "function") {
          var status = await window.petSettings.getStatus();
          health = (status && status.health) || null;
        }
      } catch (e) {
        health = null;
      }
      if (!mounted) return;
      paint(health, target);
    } finally {
      refreshInFlight = false;
    }
  }

  // Pause polling while the window is hidden (minimized / occluded) —
  // visibilityState is undefined in test harnesses, which reads as visible.
  function hiddenNow() {
    try {
      return typeof document !== "undefined" && document.visibilityState === "hidden";
    } catch (e) { return false; }
  }

  // ── 直觉模型配置卡（快直觉 / reflex）──────────────────────────────
  var reflexState = {
    baseUrl: "http://127.0.0.1:1234/v1",
    model: "lfm3-1.7b",
    apiKey: "lm-studio",
    enabled: true,
  };
  var reflexInputs = null;

  function toastReflex(msg, isError) {
    try {
      if (helpers && typeof helpers.toast === "function") {
        helpers.toast(msg, !!isError);
      } else {
        console.log("[reflex]", msg);
      }
    } catch (e) { console.log("[reflex]", msg); }
  }

  function readReflexInputs() {
    if (!reflexInputs) return { ...reflexState };
    var model = reflexState.model;
    if (reflexInputs.modelSelect) {
      var v = reflexInputs.modelSelect.value;
      if (v === "__custom__") {
        // 手动输入模式：弹出 prompt 让用户输入
        var custom = window.prompt("输入模型名称", reflexState.model);
        if (custom) model = custom.trim();
      } else if (v) {
        model = v;
      }
    }
    return {
      baseUrl: (reflexInputs.baseUrl.value || "").trim() || "http://127.0.0.1:1234/v1",
      model: model || "lfm3-1.7b",
      apiKey: (reflexInputs.apiKey.value || "").trim() || "lm-studio",
      enabled: !!reflexInputs.enabled.checked,
    };
  }

  function applyReflexState(cfg) {
    if (!cfg) return;
    reflexState = {
      baseUrl: cfg.baseUrl || reflexState.baseUrl,
      model: cfg.model || reflexState.model,
      apiKey: cfg.apiKey || reflexState.apiKey,
      enabled: cfg.enabled !== false,
    };
    if (reflexInputs) {
      if (reflexInputs.baseUrl) reflexInputs.baseUrl.value = reflexState.baseUrl;
      if (reflexInputs.modelSelect && reflexInputs.modelSelect.options) {
        // 如果当前模型不在选项中，先添加它
        var found = false;
        for (var i = 0; i < reflexInputs.modelSelect.options.length; i++) {
          if (reflexInputs.modelSelect.options[i].value === reflexState.model) {
            found = true;
            break;
          }
        }
        if (!found && reflexState.model) {
          var opt = el("option", { value: reflexState.model }, reflexState.model);
          reflexInputs.modelSelect.insertBefore(opt, reflexInputs.modelSelect.firstChild);
        }
        reflexInputs.modelSelect.value = reflexState.model;
      }
      if (reflexInputs.apiKey) reflexInputs.apiKey.value = reflexState.apiKey;
      if (reflexInputs.enabled) reflexInputs.enabled.checked = reflexState.enabled;
    }
  }

  async function loadReflexConfig() {
    try {
      if (window.petSettings && typeof window.petSettings.getReflexConfig === "function") {
        var res = await window.petSettings.getReflexConfig();
        if (res && res.ok && res.config) applyReflexState(res.config);
      }
    } catch (e) { console.warn("reflex: load failed", e); }
  }

  async function saveReflexConfig() {
    var cfg = readReflexInputs();
    try {
      if (window.petSettings && typeof window.petSettings.saveReflexConfig === "function") {
        var res = await window.petSettings.saveReflexConfig(cfg);
        if (res && res.ok) {
          applyReflexState(cfg);
          toastReflex("直觉模型配置已保存" + (res.reloaded ? "（网关已热重载）" : "（重启网关后生效）"), false);
        } else {
          toastReflex("保存失败: " + ((res && res.error) || "unknown"), true);
        }
      }
    } catch (e) {
      toastReflex("保存失败: " + String(e && e.message || e), true);
    }
  }

  function inputRow(labelText, key, placeholder) {
    var row = el("div", {
      style: {
        display: "flex",
        alignItems: "center",
        gap: "10px",
        marginBottom: "10px",
        fontSize: "13px",
      },
    });
    row.appendChild(el("span", {
      style: { minWidth: "72px", color: "var(--text-secondary, #9aa4b2)" },
    }, labelText));
    var input = el("input", {
      type: "text",
      placeholder: placeholder || "",
      style: {
        flex: "1",
        padding: "6px 10px",
        borderRadius: "6px",
        border: "1px solid var(--border, rgba(255,255,255,0.18))",
        background: "var(--bg-secondary, rgba(255,255,255,0.05))",
        color: "var(--text-primary, #fff)",
        fontSize: "13px",
      },
    });
    row.appendChild(input);
    return { row: row, input: input };
  }

  // 从 LM Studio 拉取可用模型列表（通过主进程 IPC，避免 CSP 限制）
  async function fetchLmStudioModels(baseUrl) {
    try {
      if (window.petSettings && typeof window.petSettings.getLmStudioModels === "function") {
        var res = await window.petSettings.getLmStudioModels(baseUrl);
        if (res && res.ok && Array.isArray(res.models)) {
          return res.models;
        }
      }
    } catch (e) { /* 静默失败 */ }
    return [];
  }

  function modelSelectRow(labelText) {
    var row = el("div", {
      style: {
        display: "flex",
        alignItems: "center",
        gap: "10px",
        marginBottom: "10px",
        fontSize: "13px",
      },
    });
    row.appendChild(el("span", {
      style: { minWidth: "72px", color: "var(--text-secondary, #9aa4b2)" },
    }, labelText));
    var select = el("select", {
      style: {
        flex: "1",
        padding: "6px 10px",
        borderRadius: "6px",
        border: "1px solid var(--border, rgba(255,255,255,0.18))",
        background: "var(--bg-secondary, rgba(255,255,255,0.05))",
        color: "var(--text-primary, #fff)",
        fontSize: "13px",
      },
    });
    row.appendChild(select);
    var refreshBtn = el("button", {
      type: "button",
      className: "soft-btn",
      style: { padding: "4px 10px", fontSize: "12px" },
      onClick: function () { void refreshModelList(); },
    }, "刷新");
    row.appendChild(refreshBtn);
    return { row: row, select: select, refreshBtn: refreshBtn };
  }

  async function refreshModelList() {
    if (!reflexInputs) return;
    var baseUrl = (reflexInputs.baseUrl.value || "").trim();
    if (!baseUrl) return;
    var models = await fetchLmStudioModels(baseUrl);
    var select = reflexInputs.modelSelect;
    if (!select) return;
    // 保留当前选中值
    var current = select.value || reflexState.model;
    select.innerHTML = "";
    // 添加当前模型（即使不在列表中）
    if (current) {
      var opt = el("option", { value: current }, current);
      select.appendChild(opt);
    }
    // 添加 LM Studio 模型
    models.forEach(function (m) {
      if (m !== current) {
        select.appendChild(el("option", { value: m }, m));
      }
    });
    // 添加手动输入选项
    select.appendChild(el("option", { value: "__custom__" }, "（手动输入…）"));
    select.value = current;
    if (models.length > 0) {
      toastReflex("已加载 " + models.length + " 个模型", false);
    } else {
      toastReflex("未获取到模型列表（LM Studio 未启动？）", true);
    }
  }

  function buildReflexCard() {
    var card = el("div", { className: "life-card", style: { marginTop: "14px" } });
    var head = el("div", {
      style: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        marginBottom: "12px",
      },
    });
    head.appendChild(el("div", { className: "life-section-title" }, "直觉模型（快直觉）"));
    var enableLabel = el("label", {
      style: { display: "flex", alignItems: "center", gap: "6px", fontSize: "13px", cursor: "pointer" },
    });
    var enableCheck = el("input", { type: "checkbox" });
    enableLabel.appendChild(enableCheck);
    enableLabel.appendChild(document.createTextNode("启用"));
    head.appendChild(enableLabel);
    card.appendChild(head);

    card.appendChild(el("div", {
      style: { fontSize: "12px", color: "var(--text-tertiary, #7d8590)", marginBottom: "12px", lineHeight: "1.6" },
    }, "每次主动 tick 先由这个小模型判断要不要唤醒主脑。安静时主脑零调用，省钱；只有值得思考时才醒来。"));

    var urlRow = inputRow("Base URL", "baseUrl", "http://127.0.0.1:1234/v1");
    var modelRow = modelSelectRow("模型");
    var keyRow = inputRow("API Key", "apiKey", "lm-studio（本地可留空）");
    card.appendChild(urlRow.row);
    card.appendChild(modelRow.row);
    card.appendChild(keyRow.row);

    var btnRow = el("div", { style: { display: "flex", gap: "10px", marginTop: "12px" } });
    var saveBtn = el("button", {
      type: "button",
      className: "soft-btn",
      onClick: function () { void saveReflexConfig(); },
    }, "保存配置");
    btnRow.appendChild(saveBtn);
    var testBtn = el("button", {
      type: "button",
      className: "soft-btn",
      onClick: function () {
        toastReflex("当前模型: " + readReflexInputs().model + " @ " + readReflexInputs().baseUrl, false);
      },
    }, "查看当前");
    btnRow.appendChild(testBtn);
    card.appendChild(btnRow);

    reflexInputs = {
      baseUrl: urlRow.input,
      modelSelect: modelRow.select,
      apiKey: keyRow.input,
      enabled: enableCheck,
    };
    applyReflexState(reflexState);
    return card;
  }

  function render(parent) {
    mounted = true;
    if (timer) { clearInterval(timer); timer = null; }
    if (visibilityHandler && typeof document !== "undefined"
      && typeof document.removeEventListener === "function") {
      document.removeEventListener("visibilitychange", visibilityHandler);
    }
    visibilityHandler = null;

    var wrap = el("div", { className: "pet-page-header" });
    var textCol = el("div", { className: "pet-page-header-text" });
    textCol.appendChild(el("h1", {}, t("lifeTitle")));
    textCol.appendChild(el("p", { className: "subtitle" }, t("lifeSubtitle")));
    wrap.appendChild(textCol);
    var refreshBtn = el("button", {
      type: "button",
      className: "soft-btn",
      onClick: function () { void refresh(); },
    }, t("lifeRefresh"));
    wrap.appendChild(refreshBtn);
    parent.appendChild(wrap);

    var card = el("div", { className: "life-card" });
    var bodyHost = el("div", { className: "life-body-host" });
    card.appendChild(bodyHost);
    parent.appendChild(card);

    parent.appendChild(buildReflexCard());

    parent.appendChild(el("div", { className: "life-footnote" }, t("lifeFooterNote")));

    refs = { bodyHost: bodyHost };
    void refresh();
    void loadReflexConfig().then(function () { void refreshModelList(); });
    timer = setInterval(function () {
      if (!mounted || hiddenNow()) return;
      void refresh();
    }, POLL_MS);

    if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
      visibilityHandler = function () {
        if (mounted && !hiddenNow()) void refresh();
      };
      document.addEventListener("visibilitychange", visibilityHandler);
    }
  }

  function onExit() {
    mounted = false;
    if (timer) { clearInterval(timer); timer = null; }
    if (visibilityHandler && typeof document !== "undefined"
      && typeof document.removeEventListener === "function") {
      document.removeEventListener("visibilitychange", visibilityHandler);
    }
    visibilityHandler = null;
  }

  function init(coreArg) {
    try {
      core = coreArg;
      helpers = core.helpers;
      core.tabs.life = {
        render: function (parent) { void render(parent); },
        onExit: onExit,
      };
    } catch (e) { console.warn("life: init error", e); }
  }

  root.ClawdSettingsTabLife = { init };
})(typeof globalThis !== "undefined" ? globalThis : window);
