# Shinsekai / 新世界（RachelForster）

- 仓库/主页：https://github.com/RachelForster/Shinsekai · 星标：807（fork 89）· Python（后端引擎，SQLite+FTS5+ONNX）+ React/Tauri（设置中心与桌面壳，pnpm 构建）· 许可：自定义 restrictive——公开源码、可 fork 讨论，**未经书面许可禁止再分发/发布 Release/商业使用** · 活跃度：2026-04-27 建库，最后 push 2026-11-22（当日），83 open issues，已到 2.7.0，迭代频繁
- 一句话定位：Galgame/剧情向 RPG 的开源演出平台——LLM 驱动对白与剧情，情绪联动立绘/服装/背景/BGM，带记忆遗忘曲线、角色包生态和分支剧本画布；本质是"单机 galgame 引擎 + AI 伙伴"混合体。
- 架构要点（情绪标签→立绘联动 + 角色包生态，中等优先级 6）：
  - **情绪→形象联动协议**：角色包绑定多形态形象（静态图 / MP4·WebM 透明视频 / MMD PMX+VPD·VMD / 实验性 Live2D），统一全身/半身/特写三档取景；**服装分组**（一键智能归组、手动切换/锁定、随剧情自动换装）+ 口型同步 + 说话眨眼；对话情绪驱动背景/BGM/音效/特效自动切换、弹出分支选项与好感度（-10~10）变化。
  - **立绘智能生成管线**（内容供给端）：单图生成全套喜怒哀乐立绘——LLM 规划提示词与中文标签、参考图锁定取景比例、可选 MiniMax H3 图生视频（静态选帧/透明动态/22 帧单向过渡）+ ToonOut/ISNet 抠图。**这是"用 AI 把一套立绘成本降一个数量级"的完整开源实现**。
  - **艾宾浩斯记忆衰减（源码核验 `ai/memory/ebbinghaus.py`）**：双粒度保留引擎，公式直接可读——稳定性 `S = S₀·(1 + 0.6·ln(1+recall_count))·(0.4 + 2.6·importance)`，衰减 `R(t) = exp(-Δt_eff / S)`；按记忆种类给基础天数（episodic 4.5 天 / promise 8 天 / profile 14 天 / canon ∞）；R≥0.50 渲染为"清晰"（全文）、≥0.15"模糊"（梗概）、之下"沉睡"（gist 短语）——**沉睡记忆永不物理删除，留待高相似唤醒**；超过 7 天的单次离线空档从衰减时间中扣除（离家一周回来不会失忆）；固有约定（canon/pinned）永不遗忘。
  - **角色包生态**：`.char` 角色包导入导出 + AI 辅助生成设定/简介/翻译 + 助手一句话自动建角色；社区资源站（`Shinsekai-Resource-Update` 仓库 characters.json 索引，实查 24 个角色包，百度网盘分发）+ 发行版整合包；插件 SDK（`python -m sdk.cli create` 脚手架）+ Plugin-Registry 市场仓库 + MCP（SSE/stdio）。
  - **其余**：屏幕感知 + 本地 RapidOCR 七类场景自适应发言节奏；单时间线非破坏性压缩、4合1回溯级联、剧情回放导出 MP4/WebM；ComfyUI 现场画 CG。
- UI/交互模式：Galgame 舞台（立绘+背景+对话框+分支选项）为主、圆形小图标桌宠挂件为辅（可拖动贴边 + 全局快捷键）；React/Tauri 设置中心集中管理；5 套聊天皮肤 + 可视化主题编辑器。
- 对凌凌的启示：
  - **照抄**：①**Ebbinghaus 衰减公式全套**——与我们 memory decay 器官同题，他们的三阈值双粒度（清晰/模糊/沉睡 + 沉睡不删除待唤醒）和"离线空档折扣"是现成可调参的实现，直接纳入 decay 器官参数设计；②**情绪标签→多形态资产绑定**的分层结构（取景档位 × 服装分组 × 过渡帧）可反向加固我们的 [EMOTION:] → animation 状态映射，特别吸收"22 帧单向过渡 + 无过渡自动淡入淡出"的兜底规则；③"无过渡时自动淡入淡出"这类降级策略。
  - **改造**：①**立绘生成管线**（LLM 规划提示词 + 单图生成全表情 + 自动抠图）可改造成凌凌皮肤/表情包的 AIGC 生产线，解决我们素材产能瓶颈；②角色包 `.char` 格式 + 社区索引仓库（JSON 清单 + 网盘链接，零存储成本）的生态模式，可改造为凌凌"灵魂包/器官包"分发方式。
  - **避开**：①自定义 restrictive 许可（不可抄代码，连再分发都要书面许可）；②galgame 舞台重 UI 路线与凌凌"轻挂件+桌面世界"定位不同；③百度网盘分发的社区资源合规风险。
- 来源链接：
  - [仓库 README](https://github.com/RachelForster/Shinsekai)
  - [社区资源站](https://rachelforster.github.io/Shinsekai/resources.html)（数据源 [Shinsekai-Resource-Update/characters.json](https://github.com/RachelForster/Shinsekai-Resource-Update)，实查 24 个角色包）
  - 源码核验：`ai/memory/ebbinghaus.py`（2026-11 main 分支）
