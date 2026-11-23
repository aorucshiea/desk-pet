# MiniCPM Desk Pet（OpenBMB）

- 仓库/主页：https://github.com/OpenBMB/MiniCPM-Desk-Pet · 星标：503（fork 65）· JavaScript/Electron（pet 层 fork 自 rullerzhou-afk/clawd-on-desk）+ MiniCPM5-1B-GGUF 本地模型（约 2GB 磁盘）· 许可：AGPL-3.0-only（模型权重另按 OpenBMB MiniCPM Model License）· 活跃度：2026-05-18 建库，最后 push 2026-08-18，14 open issues——**近三月未更新**
- 一句话定位：大厂出品的"本地优先"桌宠——MiniCPM5-1B 全本地对话 + 17 种编程 Agent 的状态感知反应，宠物的存在意义就是当你的编码 Agent 情绪看板。
- 架构要点（编程 Agent 感知如何实现，优先级 4，源码核验）：
  - **适配器注册表**：`clawd-on-desk/agents/registry.js` 聚合 17 个 Agent 适配器：Claude Code、Codex、Copilot CLI、Gemini CLI、Antigravity、Cursor Agent、CodeBuddy、Kiro CLI、Kimi CLI、Qwen Code、CodeWhale、opencode、Pi、OpenClaw、Hermes、Qoder、Reasonix。每个适配器是纯配置对象：进程名表（`processNames.win/mac/linux`，用于 `detectRunningAgentProcesses()` 进程扫描和一键连接引导）+ 事件源 + 事件映射 + 能力声明。
  - **两种感知通道**：①**Hook 通道**（Claude Code）：把 Agent 自身的 hook 事件（SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/Stop/SubagentStart/PreCompact/WorktreeCreate…）映射为宠物状态机状态（idle/thinking/working/error/attention/juggling/sweeping/carrying/notification/sleeping）——例如 `Stop→attention`（等你输入）、`SubagentStart→juggling`（"手忙脚乱"动画）；②**日志轮询通道**（无 hook 能力的 Agent）：`codex-log-monitor.js` 轮询 `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`，零依赖解析 JSONL，带两层重放保护（行级 timestamp 过滤 + 文件级 backfill 标记，防止启动时把历史记录误报为实时状态），从 token usage 提取**上下文占用百分比**，启发式识别审批等待（APPROVAL_HEURISTIC_MS=2000ms）。
  - **产品化包装**：设置页扫描本机已装 Agent（进程名匹配）→ 一键连接引导；"任务叙事"——Agent 会话结束时宠物用气泡 + 语音总结刚才干了什么；空闲提醒——Agent 等输入超时则摇铃动画 + 音效。安全闸门：Telegram 远程审批、移动 PWA、auto-pilot 等能力已迁移但默认关闭，需专项测试才启用。
  - **本地栈**：首启向导（环境检查→模型下载→预热）→ GGUF 推理； persona adapter（LoRA，内置 neko 风格，用 NekoQA-30K 数据集微调）；HF/ModelScope 双源智能下载。
- UI/交互模式：悬浮聊天气泡（Cmd/Ctrl+Shift+M）+ 常驻小宠；SVG 状态动画集（从上游 clawd-on-desk 继承大量状态姿态：思考/工作/错误乌云/等待/扛着东西等）；多语言 README（中/繁/日/韩）。
- 对凌凌的启示：
  - **照抄**：①**适配器 = 纯配置对象**的扩展模式（进程名表 + 事件映射 + 能力位），我们 coding-agent 状态集成应做成同款注册表，新增 Agent 零代码；②**双通道感知**：有 hook 用 hook，没 hook 轮询日志——轮询侧的重放保护（timestamp 过滤 + backfill 快照）和上下文占用百分比提取可直接抄思路；③**事件→状态→动画**映射表把我们 [EMOTION:] 协议和 Agent 状态打通（`Stop→attention` + 摇铃是最有情绪价值的一拍）；④进程扫描 + 一键连接引导的新手引导设计。
  - **改造**：他们只"看"（state-only，明确写明 report activity without taking over permissions）；我们可保持看护定位，但把"任务叙事"从一次性气泡升级为**慢直觉 LLM 的总结 + 情绪反应**（它用 0.9-1B 小模型只能做模板化叙事，我们的主线模型可以做出真正有温度的复盘）。
  - **避开**：①它 fork 上游 UI 导致 AGPL 传染 + 近三个月停更——我们坚持自研栈的必要性得到验证；②"pet 只当编码 Agent 看板"定位太窄，缺乏生命感（无记忆成长/无物理世界），正是我们器官系统的空间。
- 来源链接：
  - [仓库 README（Supported Agents / Safety Gate）](https://github.com/OpenBMB/MiniCPM-Desk-Pet)
  - [clawd-on-desk 内嵌 README（迁移范围与安全闸门）](https://github.com/OpenBMB/MiniCPM-Desk-Pet/blob/main/clawd-on-desk/README.md)
  - 源码核验：`clawd-on-desk/agents/registry.js`、`agents/claude-code.js`、`agents/codex-log-monitor.js`（2026-11 main 分支）
