# AAAAGENT（phoiex）

- 仓库/主页：https://github.com/phoiex/AAAAGENT · 星标：229（fork 29）· macOS 原生 Swift/WebKit + Windows Electron（双端独立源码树）· 许可：自定义——禁止商用、使用须署名 · 活跃度：2026-09-17 建库，最后 push 2026-10-18，0.3.0-dev.1 开发候选，更新公告密集但近一月放缓
- 一句话定位：macOS 优先的 Live2D 陪伴助手——陪伴归陪伴、干活归干活：聊天中识别工作意图 → 整理成任务卡 → 用户确认 → 转交给 DeepSeek Harness/Codex 执行，桌面宠只负责呈现状态与结果。
- 架构要点（任务转交编码 Agent 的模式，优先级 7；README 顶部 mermaid 架构图 + docs/ARCHITECTURE.md 核验）：
  - **路由模型**：输入处理（文字/ASR/可选唤醒/图像观察）→ `ROUTE{聊天/工作}` 二分——聊天走上下文组装（近期对话 + 相关记忆 + 情绪 + 允许共享的社交摘要）→ 对话回应；工作走 `CONFIRM 任务卡·用户确认` → `WORK`（T3 Code / Flow 工作台承载 Codex 与 DeepSeek Harness 固定版本连接）→ 状态与结果回流桌宠。
  - **任务转交细则**（README 能力表）：小型检索、整理类交给 Harness；规划、架构、工程任务交给 Codex；指定执行者时保留用户选择，发送前必须确认。设计纪律："日常对话不应为了'现在几点'之类的问题创建工程任务"。
  - **上下文分层纪律**（对凌凌最有价值的部分）：陪伴和工作共享一个入口，但**工程记录不塞进陪伴记忆**——最近的对话负责衔接，长期记忆负责回想，项目索引（只存名称、简短摘要和详情引用）负责找到工作上下文；完整任务内容、回执、执行记录不常驻陪伴上下文，按需读取。
  - **数据分层**：私人 `companion.sqlite`（对话/长期记忆/情绪）/ 独立社交服务 `social.sqlite`（群事件/真实回执/摘要/共享策略）/ 桌宠只读缓存 `social-cache.sqlite`（15 秒增量同步、5 分钟失联停用引用、撤回传播）——四个库物理隔离。
  - **情绪系统**：从文字与近期对话推测情绪，分别保存"用户情绪"与"桌宠心情"，每条消息冻结当时观察快照；网页管理页可查来源。
  - **其他**：记忆设计五原则（先接住最近几轮→后台异步整理不阻塞对话→情绪带来源→工程细节按需读→用户可管理）；微信/QQ 渠道接入（独立社交服务 + 持久队列 + 幂等去重 + 真实发送回执后才生成摘要）；Windows 三套本地 ASR/TTS 实测（GPT-SoVITS/Kokoro/Qwen3-TTS 等）+ Ollama 4B 前台对话（坦承 4B 有算术与召回错误，不算稳定）。
- UI/交互模式：Live2D 桌宠（暂用量贩第三方模型 + PC2005-cloud/dsh-pet 视频素材，自研"DeepSeek 大肥鱼"Live2D 制作中）+ 网页管理端（Prompt/记忆/召回记录/上下文/语音/表现预设全部可视化）+ T3 Code / Flow 工作台（对话节点组织项目）。
- 对凌凌的启示：
  - **照抄**：①**"陪伴看护 + 确认后转交"的混合定位**——我们 agent-status 集成目前是纯监控（watch），AAAAGENT 证明"宠物可以把用户的一句话升级成任务卡 → 确认 → 派给 Harness"是一条可行的进阶路径，且与我们已有的 DSH 生态天然契合（可直接成为凌凌的 agent-integration 路线图第二阶段）；②**工程记录与陪伴记忆的分层纪律**（项目索引只存摘要+引用，回执不进陪伴上下文）——直接用于我们 coding-agent 状态集成的上下文预算设计；③"日常闲聊不建工程任务"的路由护栏文案与实现位置。
  - **改造**：其任务卡确认流可改造为我们 impulse organ 的出口之一（冲动 → 任务卡 → 用户确认 → 派活），比他们的纯意图识别多一层"宠物主动性"。
  - **避开**：①双端双源码树（Swift+Electron 各写一遍）的维护成本——我们坚持 Tauri 单栈正确；②把 Live2D 素材授权悬在外部（量贩模型不能分发）导致演示与发布长期受限——我们素材授权必须自始干净；③自定义禁商许可限制代码借鉴。
- 来源链接：
  - [仓库 README（整体架构 mermaid 图 + 更新公告）](https://github.com/phoiex/AAAAGENT)
  - [docs/ARCHITECTURE.md（数据分层与模块入口）](https://github.com/phoiex/AAAAGENT/blob/main/docs/ARCHITECTURE.md)
  - [docs/MEMORY.md 记忆与上下文](https://github.com/phoiex/AAAAGENT/blob/main/docs/MEMORY.md)（README 索引引述）
