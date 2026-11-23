# 提交计划（2026-11-23，待 reflex-wiring / ui-vitals 全绿后执行）

> 原则：按"文件可分性"分组，不按时间。server.py 承载了多个特性（节奏+体征+reflex+标签），无法按文件拆分 → 归入同一个核心提交。提交前跑全量双套件。

## 前置条件
- [ ] reflex-wiring 全绿（含 6 条修正：主题切换重建、死亡台账复活、整理槽位保护、工具收窄守卫、门禁全覆盖、recap 100 轮注入）
- [ ] ui-vitals 全绿（生命体征设置卡 + i18n）
- [ ] `pytest -q` 0 failures / `npm test` 0 failures
- [ ] 冒烟：启动 app 观察 300s tick → reflex quiet → 台账 fast 条目增长 → /api/health 有 vitals/ledger

## 提交分组（建议顺序）

1. **`fix(gateway): 常量温度注入与整理轮生命周期修复`**
   - 主题切换 vitals/ledger 重建、死亡台账复活、整理槽位 proactive-only、工具收窄守卫、reflex 门禁全覆盖、短路 sleep_state 事件
   - 文件：server.py、think_filter.py、test_gateway_reflex_wiring.py
   （若 reflex-wiring 把这些与其特性实现交织，则并入第 2 条，不硬拆）

2. **`feat(gateway): 生命体征与快慢直觉闭环（节奏/电量/血量/睡眠/台账/reflex）`**
   - vitals.py、ledger.py、decision/reflex.py（新）
   - server.py 三档节奏 + 温度合成 + 工具中文标签 + 整理轮
   - tests: test_gateway_tempo.py, test_vitals.py, test_ledger.py, test_reflex.py, test_gateway_vitals_wiring.py + 标签钉住的既有测试
   - think_filter.py 正则放宽

3. **`feat(shell): 渲染端节奏三档 + next_check_s + 睡眠指示 + 生命体征设置卡`**
   - pet-chat-renderer.js（TEMPO_RANGES/next_check_s/sleep_state/正则放宽）
   - settings-tab-life.js（新）+ settings-renderer/css/i18n
   - tests: tempo-ranges.test.js（新）、life-round-ownership 默认值更新

4. **`fix(deepseek_web): 扩展 0.4.2 — 会话修复/中文清理/短路提示`**（10-27 会话遗留）
   - deepseek_web/extension/*、bridge.py、README

5. **`feat(evolve): 自进化工作台 M1 + 虚拟桌面守卫`**（11-19 会话遗留，文件完全可分）
   - gateway/evolve/*、clawd-on-desk/src/virtual-desktop-guard.js、vendor/virtual-desktop/、相关测试、adapters/.manifest.json

6. **`docs: 竞品调研 28 卡 + UI 路线图 + 架构文档五系统 + BACKLOG B-17/18`**
   - docs/competitors/*（21 文件）、ui-roadmap-2026-11.md、autonomy-architecture.md、BACKLOG-bugs.md、pet-sidecar/README.md（若更新）

7. **`chore: 其余 11-19 s-fix 遗留`**（persona/cortex/openai/loader 决策修正等若与 2 无法分离则并入 2）

## 不提交
- E:\deskpet-references\（仓库外）
- E:\deskpet-research 临时脚本（如存在）
- .tmp-* 目录、models/、venv

## 提交后
- push origin master（用户惯例）
- 渲染端收尾任务清单（B-17 双睡眠统一、HUD 徽章、Live2D 试点待 Cubism 许可核验）
