# UI 与功能路线（2026-11-23，参照竞品调研）

> 参照项目已克隆到 `E:\deskpet-references\`（仓库外，不污染 git）：
> - `open-llm-vtuber/` — [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber)：语音+Live2D 桌宠的功能标杆
> - `dsh-chatting-pet/` — [dsh-chatting-pet](https://github.com/flhy666/dsh-chatting-pet)：DSH 生态桌宠（Edge 免费 TTS / 角色卡导入 / 内容源喂记忆）
> - `desk-pet-rust/` — [Desk-Pet](https://github.com/V1rtual-Klavte/Desk-Pet)：Rust 无 WebView 桌宠（记忆外部基准 96%+ / MCP 工具）
> 另有闭源参照：[AI Desktop Pet (Steam)](https://store.steampowered.com/app/4227700/)（GGUF 内置 / VRM / 语音克隆 / Workshop）

## 一、竞品矩阵

| 能力 | 凌凌（现状） | OLLV | dsh-pet | Desk-Pet(Rust) | Steam Pet |
|---|---|---|---|---|---|
| 语音 TTS/ASR | ❌ | ✅ 实时+打断 | ✅ Edge免费+按键说话 | — | ✅ +声音克隆 |
| Live2D/3D 形象 | ❌ GIF 主题 | ✅ Cubism 5+表情映射 | ❌ | — | ✅ VRM/Spine |
| 触摸反馈 | 仅摸头唤醒 | ✅ | — | — | ✅ 点击反应 |
| 主动搭话 | ✅ 生命轮 | ✅ | ✅ 掷骰模式 | ✅ | ✅ 空闲自言自语 |
| 桌面感知 | ✅ 视锥+截图+刚体 | ✅ 视觉感知 | ✅ 桌面观察 | ✅ | ✅ 看屏幕 |
| 长期记忆 | ✅ 多层+器官 | ⚠ 移除重做中 | ✅ 多源 | ✅ **96% 基准** | — |
| 工具/自进化 | ✅ **器官+自进化M1** | ✅ MCP | — | ✅ MCP/Skills | — |
| 桌面物理 | ✅ **独有：刚体推拉踢拎** | ❌ | ❌ | ❌ | — |
| 情绪系统 | ✅ **双通道+器官** | 表情映射 | — | — | — |
| 生命体征 | 🆕 电量/血量/睡眠 | ❌ | ❌ | ❌ | ❌ |
| 编程agent集成 | ✅ **独有** | ❌ | ❌ | ❌ | ❌ |
| 社区内容分发 | 手动主题 | — | 角色卡导入 | — | ✅ Workshop |
| 记忆外部基准 | ❌ | ❌ | ❌ | ✅ | ❌ |

**结论**：凌凌的独有面（潜意识器官/桌面物理/生命体征/agent 集成）没有竞品跟进；**最大差距是语音**，其次是形象表现力和触摸反馈丰富度。

## 二、UI 专项差距（用户直接看见的）

1. **新系统零 UI**：节奏/电量/血量/睡眠/直觉模型刚落地，设置里没有任何入口；用户看不到血条、电量、睡眠状态。
2. **睡眠不可见**：睡觉时桌宠动画不会变化（主题 schema 已有 sleeping state，只差接线）。
3. **对话气泡是纯文本**：竞品普遍有表情/语音/形象反馈。
4. **记忆不可浏览**：memory viewer 是文件列表，没有"日记/时间线"形态（台账落地后正好做）。

## 三、路线（2026-11-23 修订：感官层提为 P0——用户裁定"记忆公式类架构优化"让位）

### P0 · GUI 优化规格（2026-11-23 深挖结论：基建已就绪，缺内容与两处渲染升级）

**发现：情绪→身体动画管线 100% 存在，缺的只是内容。**
- `state.js` `currentEmotion` 从 [EMOTION:] 标签更新（网关转发）；
- `state-visual-resolver.js:86 pickStateFileByEmotion` 偏好情绪标签变体（"纯增量——主题没有变体就用基础池"）；
- `theme-loader.js` 有 per-theme emotion-override 目录（`{userData}/emotion-overrides/<themeId>/`）；
- **mochi 零情绪变体** —— `gen-mochi-theme.py` 已内置全部所需原语（`face(eyes,mouth)` 参数化、`heart/sparkle/qmark/bang/drop` 道具）。
- **实现配方（约 150 行 + theme.json 变体对象）**：扩展生成器生成 8 情绪 idle 变体（happy=弯眼+爱心 / sad=垂耳+泪滴 / curious=歪头+问号 / angry=叹号+红脸 / surprised=圆眼 / love=爱心 / tired=半闭眼+zzz），states.idle 加 `{file, emotion}` 变体对象数组 → [EMOTION:] 自动驱动表情。**零新基建。**

**气泡升级（借 OLLV-Web，组件已克隆 E:\deskpet-references\ollv-web）：**
- 参照组件：`canvas/subtitle.tsx`（字幕覆盖层——比气泡更"陪伴感"的说话方式）、`footer/ai-state-indicator.tsx`（AI 状态常驻指示）、`chat-bubble.tsx`、`electron/input-subtitle.tsx`；
- speak 主文本目前是纯打字机文本，markdown 只用于 last-reply 区域 → 升级为**渐进 markdown 渲染**；
- 字幕条模式作为气泡的替代形态（桌宠下方一行字幕，适合 Live2D 形象）。

**生命体征卡**（ui-vitals 进行中）+ **HUD 徽章**（下一步）。

### P0 · 声音（organ_voice，网关器官——OLLV 已给出完整配方）
- **TTS 先行**：`edge-tts` pip 包（免费、中文神经音色 zh-CN-XiaoxiaoNeural 等），OLLV 的 edge_tts.py 实现仅 ~20 行；按句切分流式合成——**生成时就开始说话，不等全文**；[EMOTION:happy/sad] → rate/pitch 微调出语气差异。
- **ASR 听**：faster-whisper 或 sherpa-onnx（本地离线，OLLV 均有现成适配器），长按快捷键说话 → 识别 → 进对话；语音打断（barge-in）后置。
- 架构照抄 OLLV 的工厂模式：`tts_factory` / `asr_factory`，每个后端一个 ~50 行适配器，免费路线（edge）与本地路线（whisper）并行，云端（cosyvoice/fish/音色克隆）可选。
- 生命周期：做成 **organ_voice 器官**（热插拔、Telegram/web-panel 通吃），不是壳功能。

### P0 · Live2D 试点（形象）
- 渲染：`pixi-live2d-display`（1.5k★）+ pixi.js WebGL 放进现有透明 BrowserWindow；GIF 主题保留为默认回退，Live2D 作为**新主题类型**（theme schema 扩展）。
- **内容生产**：live2d-automation MCP 单图 → 完整 Live2D 模型（research-components2 核验中）——解决"没有模型可用的最大瓶颈"。
- **情绪→表情映射**：OLLV 的 `model_dict.json emotionMap` 协议——我们的 [EMOTION:] 标签直接映射表情索引，管线零改动。
- ⚠️ Cubism Core SDK 许可是商用门槛（research-components2 核验中）。

### P0 · 生命体征 UI（配套刚落地的后端）
- **设置 → 生命体征卡**：电量条+血条+睡眠状态+节奏档+直觉模型配置（reflex 的 providers.json 块的 UI 面），沿用 `settings-tab-web-model.js` 的卡片模式；数据源 `/api/health` 轮询（字段已由接线代理添加）。
- **睡眠可视化**：vitals.asleep 时主题 sleeping 动画（schema 已支持 fallbackTo）+ 拖拽锚定（hit-window 忽略拖动）+ 气泡"Zzz"；唤醒阶梯的体感文案网关已注入。
- **HUD 徽章**：桌宠旁常驻小血条+电量条（低电量/低血量时才显眼，平时隐藏——不做成仪表盘）。

### P1 · 语音（最大差距，有零成本路径）
- **TTS 朗读先行**：Edge 神经语音免费（dsh-chatting-pet 已在 DSH 生态验证），气泡文本 → 语音输出，情绪标签 [EMOTION:] → 音色/语速微调。
- **按键说话 ASR**：长按快捷键 or 右键说话 → 识别 → 进对话；打断（barge-in）后置。
- 语音输出挂气泡流式结束后整句播报，不逐字 TTS。

### P1 · 酒馆 v2 角色卡导入（内容生态，成本极低）
- 人格 = persona 文本包；SillyTavern v2 卡字段 → persona + 开场白 + 情绪示例。直接吃到整个社区角色库，不动 LoRA 体系（LoRA 留给"声音/形象"层）。

### P1 · 触摸反应库
- 摸头/戳/拍/拖 的反应动画 + 快直觉驱动的反应词（现有 touch wake 只有一条提示词）；反应词走快直觉（便宜），动画走主题 touch 状态。

### P2
- **记忆外部基准**：学 Desk-Pet 跑 LongMemEval-S / MemoryBank-cn，给记忆器官一个外部校准分数（现在全是自评）。
- **Live2D 试点**：Cubism SDK 成本评估，情绪→表情映射复用 [EMOTION:] 协议。
- **内容刺激源**：RSS/B站 API/桌面观察结果喂台账（快直觉的"新闻感"）。
- **主题 WorkShop 化**：BodySpec + 主题包一键分享（配合 BodySpec 落地）。

### P3
- VRM/3D 形象、语音克隆、VN 模式（不同品类，不追）。

## 四、UI 借鉴清单（研究卡 → 具体控件，2026-11-23 收敛：只借 UI）

> 依据 docs/competitors/ 研究卡（VPet 数值逆向、DyberPet 素材文档、OLLV 设置页、Coopanion/HermesPet/Live2DPet 产品形态）。

| 借鉴 | 来源 | 落到凌凌哪里 |
|---|---|---|
| 数值条语义：吃半结算+缓释、溢出有去处 | VPet GameSave.cs | 电量/血量条旁注明"缓释中"；饱食溢出→掉血映射到 vitals 扣费语义 |
| 概率动画矩阵（动作×状态×时段） | DyberPet 素材文档 | BodySpec 步态计划的数据驱动格式（杜绝 switch-case） |
| 气泡触发器："1 秒点 7 次→烦了"、"专注时拍拍→'你该专注'" | DyberPet 9 种触发器 | touch 反应词库（快直觉驱动），防刷屏合并 |
| 设置页布局：左右分区+即时生效 | OLLV web UI | 生命体征设置卡（settings-tab-life） |
| 随 AI 模式换装 + 任务完成主动提醒 | HermesPet | agent 集成已有，补"换装"层（主题变体） |
| 扔球物理道具 | vscode-pets | 一个道具撑起"和宠物玩"——刚体系统的玩具扩展 |
| 窗沿攀爬/移动平台惯性/回收站进食 | 啾糯桌宠 | 刚体系统升级清单（学设计，⚠️ PolyForm 许可不抄代码） |

## 四、架构注意

- 语音（P1）必须是**网关器官**（organ_voice），不是壳功能——这样 TTS/ASR 才能进快直觉/慢直觉的管线，且 Telegram/web-panel 通吃。
- 触摸反应库走主题 schema 扩展（touch 状态 + 反应词表），不要硬编码。
- 所有 P0/P1 UI 数据源统一走 `/api/health` + SSE 事件，禁止各组件自行读 vitals.json。
