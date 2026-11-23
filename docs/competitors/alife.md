# Alife（BDFFZI）

- 仓库/主页：https://github.com/BDFFZI/Alife · 星标：235（fork 33）· C# / .NET 10 + Blazor/AntDesign（Win 用 WinForms+WebView2，安卓用 MAUI）+ Python 3.12 本地模型 · 许可：AGPL-3.0 · 活跃度：2026-03-15 建库，最后 push 2026-11-22（当日仍在更新），高频迭代
- 一句话定位：专为"赛博生命"设计的全插件化 AI Agent 框架——客户端是空壳，一切功能皆插件，AI 通过自带 MCP 服务自己写插件、自己编译热重载，靠永久唯一会话和多级压缩记忆把token 成本压到极低。
- 架构要点（"AI 写插件"与永久唯一会话，优先级 1）：
  - **自我进化的真实机制（源码验证）**：`AlifeMcpTools.cs` 把整个框架 MCP 化（127.0.0.1:18765/mcp），提供角色管理、活动管理、插件装载/市场安装、对话与上下文查看的完整控制面；内置 AI 装"开发者模式"插件后，`DeveloperService` 以 **MCP 客户端连回自家 MCP 服务器**（`McpUtility.ConnectHttpAsync`），把全部控制工具以 XML 函数形式注册进对话——AI 于是能读写自身项目、写插件源码、管理错误。外部 Agent（codex 等）也能接同一个 MCP 端点替用户开发。
  - **热编译管线**：`Alife.PluginSystem` 用 Roslyn `CSharpCompilation` 直接把插件 .cs 源码编成 DLL（自动注入平台预处理符号、自动收集 app 全部程序集作为编译引用），经 `PluginLoadContext` 隔离加载，配依赖解析、插件市场（独立仓库 Alife.PluginMarket）、FileSystem/Zip 安装器。编译失败会把完整诊断错误抛回给 AI 修正（闭环）。
  - **永久唯一会话**：不是无限长上下文——`AlifeContext` 分区维护 + 会话有意复用保持稳定（提示词缓存命中高达 95%，官方 DeepSeek 通道日均成本不到 2 元）；记忆不靠 AI 自主存取，而是"多级 cache 式自动压缩"（Memory 插件：多级指数压缩 + 向量化 + DuckDB 检索），对话经历呈现"伪常驻"效果。人工手搓提示词 + 原生文本 XML 函数调用（非标准 function calling，无模型门槛、透明可控、省 token）。
  - **生态位**：全功能皆插件（桌宠 Live2D、视觉、语音、浏览器、游戏陪玩、QQ、棋牌/MC 陪玩），角色多开互联（VirtualWorld 跨角色通讯），SystemEvent 阶梯定时事件驱动空闲自主行为；异常隔离、MCP/Skill 双生态接入。
- UI/交互模式：Live2D 桌宠窗体（WebView2 承载，AI 可控身体）；对话看板 + 上下文片段专用 UI 完整暴露 AI 执行过程（白盒化）；Windows/Android 双端数据互通、插件互通；无初始人设，人格完全靠长期会话成长。
- 对凌凌的启示：
  - **照抄**：①"框架自身 MCP 化"作为自进化通道——我们的 evolve/ 工作台可以暴露一个 `deskpai-mcp`，让主 LLM（慢直觉）和外部编码 Agent 用同一套工具读写器官代码、体检报告、插件注册表，这是比我们现有 workbench 更通用的接口设计；②95% 缓存命中思路——永久唯一会话的本质是**上下文结构稳定性**（分区维护 + 稳定前缀 + 自动压缩），直接反哺我们的 theme-soul 隔离设计与成本模型（我们多灵魂并行,更需会话前缀稳定）；③多级指数压缩 + 向量化检索的"伪常驻"替代无限上下文。
  - **改造**：①他们的 AI 写插件是"运行时直接热编译进主进程"——异常隔离靠 AssemblyLoadContext，但没有我们的 worktree 隔离、evaluator 评审、人工审批闸门；我们应保持"AI 写 → 评估 → 人批 → 隔离加载"链路，只借鉴其**编译错误自动回喂 AI**的闭环细节；②其"不偷用子代理、AI 亲历亲为"与我们 fast/slow 双直觉相反——可写进差异化对比：我们用小模型做快直觉,省 token 且低延迟。
  - **避开**：①AGPL-3.0 传染（不可抄代码，设计可借鉴）；②"无初始人设纯成长"路线启动成本高、早期体验空窗，与我们 theme-soul 预置灵魂冲突；③全自动环境依赖下载安装（用户侧等一小时）的体验坑。
- 来源链接：
  - [仓库 README（框架特色/插件目录/MCP 说明）](https://github.com/BDFFZI/Alife)
  - [DeepWiki: Alife Overview](https://deepwiki.com/BDFFZI/Alife/1-alife-overview)（Character/ChatBot/ChatActivity 三层运行时模型）
  - 源码核验：`Sources/Alife.Function/Alife.Function.Developer/DeveloperService.cs`、`Sources/Alife/Alife.PluginSystem/PluginContext/CSharpCompiler.cs`、`PluginLoadContext.cs`（2026-11 master 分支）
