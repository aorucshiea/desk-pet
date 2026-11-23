# HermesPet

- 仓库：https://github.com/basionwang-bot/HermesPet · 星标：**603**（2026-11-22，GitHub API 实测） · 语言/栈：Swift 6 + SwiftUI，macOS 14+，内存约 45MB；⚠️ **主表两处记录有误**：①公共仓库是产品展示仓库——"© 2024–2026 Basion Wang. All rights reserved."，**无开源许可**（非 Apache-2.0），不可借鉴代码只能参考产品；②主语言 API 识别为 TypeScript（仓库含 Windows 尝鲜版 Electron+TS 代码），桌面本体是 Swift · 活跃度：高（最近推送 2026-11-10，DMG 经 Apple 公证，Team ID R34KL4X4D9）
- 一句话定位："AI Mission Control"——把 AI 终端群（Claude Code/Codex 等）的状态住进 MacBook 刘海，桌宠是任务完成提醒的化身。
- 架构/功能要点（README 全读 + SkillsMP 技能文档交叉验证）：
  - **"5 引擎"演进为"7 类 AI 节点"**：Cloud/在线 AI（内置 DeepSeek/智谱/Kimi/MiniMax/OpenAI 等 13 家预设，DMG 内嵌 opencode runtime 处理 SSE）、Hermes（OpenAI 兼容网关）、OpenClaw、Claude Code、Codex、QwenCode、Kimi——后四者**复用本机 CLI 登录态**（检测 `/usr/bin/which claude` 等判断可用性），未安装不阻塞基础使用。
  - **真并行架构**：每条对话独立绑定一个引擎，最多 8 个对话同时后台运行（⌘1~⌘8 切换）；引擎抽象为 `AIServiceProtocol` 协议，新引擎 = 一个 conform 实现。
  - 可观察性：不只给"正在思考"，而是把文件读取、命令执行、工具调用、错误状态统一呈现——AI 终端状态集中展示（连接状态/执行步骤/系统负载）。
  - 系统遥测舱：CPU/内存/温度/存储/网速 + 各 AI 节点连接状态实时面板。
  - 跨设备：iPhone 配套端（Agent 任务看板/批准高风险操作/传文件）+ Apple Watch 轻状态，开发中。
- UI/交互模式：
  - **刘海窗口 UX**：刘海下方是实时任务入口；鼠标碰到桌宠后**侧边栏从屏幕边缘滑出**（近侧=常用操作，另一侧=系统诊断），不打断当前工作。
  - 桌宠本体：**5 只像素风桌宠随 AI 模式切换**（切到 Claude Code 换一只、切到 Codex 换一只）+ 任务完成时灵动岛式提醒；可接收拖拽的图片/文件。
  - 快捷键体系：⌘⇧H 显隐聊天/⌘⇧J 截图提问/⌘⇧V 按住说话/⌘⇧Space 快问浮窗/⌘⇧L 连续语音陪聊/⌘⇧G 知识云图。
  - 知识云图：长期对话转成可搜索的主题星图（本机保留，按关键词/主题/重要程度检索）。
- 对凌凌的启示：
  - **照抄**：①**桌宠随 AI 模式换装**——凌凌接了 dsh 多 Agent 后，"当前在跑哪个 Agent → 宠物换对应形态/配色"是零成本的角色感来源；②**任务完成 → 桌宠主动提醒**的闭环（我们的 capture_screen/ledger 感知应长出这个出口）；③侧边栏"从屏幕边缘滑出"替代独立窗口（比弹窗温和）；④拖文件给桌宠直接进上下文。
  - **改造**：它的 7 节点接入方式里"复用本机 CLI 登录态"对凌凌同样成立（检测本机 dsh/codex 可用性，未装不阻塞）；知识云图 → 凌凌的记忆 ledger 可视化成星图。
  - **避开**：闭源免费+赞助商 API 中转的商业模式（MoreNeed API 赞助位嵌入 README，产品信用透支）；刘海 UX 是 macOS 专属，凌凌面向 Windows 不复用形态但可借鉴"屏幕边缘滑出操作台"。
- 来源链接：[GitHub README](https://github.com/basionwang-bot/HermesPet)（存档 `_raw/hermespet.md`）、[GitHub API](https://api.github.com/repos/basionwang-bot/HermesPet)、[官网 hermespet.cc](https://hermespet.cc)、[SourcePulse 项目页](https://www.sourcepulse.org/projects/30299053)、[SkillsMP 引擎扩展文档](https://skillsmp.com/creators/reason-machines/hermes-skills/skills-hermespet-macos-ai-companion)
