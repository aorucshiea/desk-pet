# FaustBot（liwusen / FaustBot-llm-vtuber）

- 仓库/主页：https://github.com/liwusen/FaustBot-llm-vtuber · 星标：45（fork 4）· Python（LangChain/LangGraph）+ Electron 前端 · 许可：GPL-3.0 · 活跃度：2026-03-14 建库，最后 push 2026-11-15，已发布 FaustBot 3 "Homunculus"，带 release 工作流；配套生态 Gesellschaft（AI 社区 + 模块市场）
- 一句话定位：AI 驱动的 Vtuber/桌面伙伴，核心卖点"能力是长出来的"——Agile Engine 让她在发现需求后自己查资料、写 Python 模块扩展自己，配上三合一混合记忆和空闲时自动整理记忆的 Araya 管家代理。
- 架构要点（自进化引擎 + 三合一记忆，优先级 2）：
  - **Agile Engine（AI 自写模块）**：触发点是"发现需求"——检测到你打开某游戏，主动查该游戏的开放接口，自己完成接入并播报对局数据。闭环三件套：浏览器自主操作（调研）+ 虚拟文件系统（落盘）+ 长期记忆（记住装过什么）。模块无需安装/配置，几分钟后"长出"新能力；写好的 Agile 模块可上传到 Gesellschaft 市场共享（`npx gesellschaft` CLI，AI 自己学会全部命令后自行登录分享）。标注为"实验性"，配套"完善的自我进化 SKILL"。
  - **三合一记忆**：向量语义检索 + BM25 关键词 + 知识图谱（图/生成树存储），写入即自动抽取实体关系并去重；检索带 2-hop BFS 图谱联想 + 树状目录组织。
  - **Araya 维护代理**：独立运行的记忆管家，空闲时归纳索引、去重、修剪冗余关系、计算重要性、打标签、写审计日志——"你休息时她在替你整理回忆"。这是把记忆 decay/整理做成**独立常驻代理**而非后台函数。
  - **Agent 底盘**：LangGraph 编排，命名子代理（可分配不同工具权限和人设、异步派活）、Trigger 触发器（定时/周期/事件/Python 条件表达式四种，让她自己"醒来"）、安全系统（权限限制 + 模型命令审核）。
  - **其他招牌**：歌台（UVR 人声分离 + Seed-VC 音色转换，用自己的声音翻唱）；灵动窗口（AI 现场写 HTML 小游戏，与 AI 双向通信，非模板）；Desktop Mood 情景感知（SMTC 正在听的歌、前台窗口、熬夜、低电量→主动开口）。
- UI/交互模式：Live2D/VRM 形象 + Image 模式（几张立绘/表情/口型图零建模拼形象）；多角色 = 一组可编辑 Markdown（人设/核心记忆/任务）；插件市场 + Skill 市场（兼容 Openclaw/ClawHub）+ MCP 客户端；B 站直播弹幕接入。
- 对凌凌的启示：
  - **照抄**：①**Trigger 触发器四类型**（定时/周期/事件/Python 条件表达式）——比我们的 impulse organ 的事件源更完整，Python 条件表达式触发器（"低电量且深夜且我三天没理她"）值得直接进我们的自主行为调度；②**Araya 独立记忆管家代理**模式——我们的 dream/decay 器官可升级为同构的"睡眠期独立代理"，空闲触发、写审计日志、可查看整理记录；③写入即抽取实体关系 + 图谱 2-hop 联想（resonance 器官可借用图谱联想替代纯向量召回）。
  - **改造**：①Agile Engine 与我们 evolve/ 的本质差异：他们**无评审直装运行**（实验性、赌安全性），我们走"AI 写 → worktree 隔离 → evaluator → 人工审批"的 M1 路线——对比后我们更稳，但他们**虚拟文件系统 + 需求发现自动触发**的入口设计可改造吸收（把"发现需求"作为我们 workbench 的自动立项信号之一）；②Gesellschaft "AI 自己上传分享模块"的社区形态可远期借鉴为凌凌器官市场的 AI 自治版。
  - **避开**：①GPL-3.0 传染（不可直接移植代码）；②功能面铺得太宽（歌台/直播/MC/浏览器），维护面巨大而星标仅 45——提醒我们差异化聚焦物理+器官而非全能。
- 来源链接：
  - [仓库 README（Agile Engine/记忆/Trigger 全清单）](https://github.com/liwusen/FaustBot-llm-vtuber)
  - [觉醒AI知识库架构分析：FaustBot 会自己写模块进化的桌面 AI 伙伴](https://www.jxxy.net/ai/articles/a-gh-20260314-93352/)
  - [Gesellschaft 社区仓库](https://github.com/liwusen/gesellschaft)
