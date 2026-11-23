# ZifaMem

- 仓库/文档：[GitHub zifacorp/zifamem](https://github.com/zifacorp/zifamem) · [arXiv 2607.17564 论文](https://arxiv.org/abs/2607.17564) · [中文 README](https://github.com/zifacorp/zifamem/blob/main/docs/i18n/zh-CN.md) · 星标：17（GitHub API 读数 2026-11-23）· 语言：Python（≥3.10）· 许可：**未定（README 明写 "To be announced"，无 LICENSE 文件）** · 最近活跃：2026-07-21 push（论文 2026-07-20 提交后随即开源，此后约 4 个月无更新，alpha 状态）
- 一句话定位：ZifaCorp 配论文开源的**情绪长期记忆 alpha SDK**——把 AI 伙伴的对话组织成 L1 会话缓冲 → L2 会话摘要 → L3 情绪标注长期记忆 → L4 用户模型的四层结构，用"强化/衰减/遗忘"生命周期代替无限堆 transcript，是凌凌记忆体系最直接的平行演化对照物。
- 架构要点：
  1. **四层记忆栈**（README Implementation Status 已实现）：L1 `record_turn` 滚动会话缓冲；L2 `end_session` 生成会话摘要；L3 长期记忆记录（带 category、importance、strength、evidence、情绪信号）；L4 从身份/偏好/边界/冲突/脆弱时刻/意义时刻记忆中提炼**用户模型**。默认零依赖，存储用 `InMemoryStore`/`JsonMemoryStore`，生产数据库与向量检索仍在 TODO。
  2. **情绪编码进记忆本体而非独立模块**（论文 §2.1）：记忆携带 mood/sentiment/intensity/trust/comfort/conflict/attachment/boundaries 八类情绪信号；论文明确"情绪历史不存在于单独的情绪模块里，而是作为 tag 参与巩固与检索"。升级为长期记忆的门槛是布尔门：`importance ≥ 0.5 或 该会话记录过情绪峰值（had_emotional_peak）`（论文式 1，并自我纠正了旧稿的"连续强度阈值"说法）。
  3. **检索公式**（论文式 2，已评估版本）：`score = 0.35·语义相似 + 0.30·记忆强度 + 0.35·importance`；强度在检索时由**年龄、访问次数、重要性、情绪元数据**重算（即时间衰减 + 使用强化）。README 补充 alpha 实现还叠加 recency decay 与 emotional intensity。论文特别声明评估路径里**没有**单独的 0.10 情绪强度权重项——防止实现与论文漂移。
  4. **生命周期 API**：手动 `remember / reinforce / weaken / forget` 已实现；自动 merge 与 reflection 循环在 TODO。LLM 抽取为可选（`OpenAICompatibleProvider`），写入 L3 前做 JSON 校验 + **用户事实证据过滤**（防幻觉记忆），失败则回落到零依赖启发式抽取器。
  5. **论文实验结论（预注册协议）**：结构化记忆 vs 同参数喂全量原始历史的"部署诚实对照"，四骨干（Claude-Sonnet-4.6 / Qwen3.6 / Doubao-Seed-1.8 / GLM-5.2）汇总情绪智能 **+11.4%**（95% CI 6.3–17.1%），Claude 角色保持相对 +42%；与 Mem0（mem0ai 2.0.12 + Doubao embedding + 本地 Qdrant）在主终点**统计等价（±5pp）**；简单"过滤逐字检索"（10 轮分块 top-5 向量检索）在短历史偏好任务上是**被低估的强基线**。
  6. **关键阴性结果**：加了结构化记忆后，独立情绪状态机（含 DyAffect 置信自适应估计器、二阶 VAD 动力学）**在全部 5 个终点上无可测增益**；多轮情绪上下文比单轮快照 +39% 净偏好（仅探索性）。论文还做了机械验证：旧的"记忆反馈力"实现是开环非闭环吸引子、快进用了丢弃速度的单指数近似——**看起来合理的情绪动力学实现起来很容易走样**。
- 可借鉴：
  - 【照抄】**情绪峰值提升门**：`importance≥0.5 OR had_emotional_peak` 的布尔门槛写进凌凌 events→ledger 的晋升逻辑——比连续阈值简单、可解释、且是被实测过的设计。对应我们现状：events+decay 只有衰减没有"情绪峰值豁免衰减"。
  - 【照抄】**用户事实证据过滤**：LLM 抽取的记忆必须附带原文证据（evidence）才落库，provider 失败回落启发式。凌凌的记忆文件夹写入路径应加同款校验，防止模型编造"用户说过…"。
  - 【改造】ZifaMem 四层 ↔ 凌凌五层对照：L1≈500-round ledger、L2≈DSTATE 滚动摘要、L3≈events+decay、L4≈记忆文件夹；凌凌独有的只有 resonance（跨记忆共振）。缺口在：我们的事件缺八类情绪信号字段、缺"重要性+强度+访问次数"重算的检索打分（0.35/0.30/0.35 权重可直接作起点 A/B）、缺强化/弱化/遗忘 API（现在多半只删不改）。
  - 【评估】论文最值钱的话："情绪信息 ≠ 情绪状态机"——记忆里带情绪 tag 之后，独立 VAD 状态机零增益。**这直接支持凌凌不做独立情绪引擎、把情绪塞进记忆表示的路线**；也解释了为何 resonance 值得做（它是记忆间关系而非并行状态机）。`skills/zifamem-memory-audit` 的记忆安全审计清单（抽取安全/证据/泄露）可抄成我们的自检 skill。
  - 【评估】评测方法论：固定 judge（温度 0、thinking 关、3 次取均值）、配对 bootstrap、预注册主终点——凌凌将来做记忆系统 A/B 时照这套流程设计，避免自欺。
- 要避开：
  - **不能抄代码**：无 LICENSE = 默认全权保留，17 星 alpha 项目且"最终许可仍在准备"。只读设计思想与论文公式，等 license 落地再考虑依赖。
  - 别把论文的 +11.4% 当成"结构化记忆普遍更优"：它自己承认在短历史偏好任务上输给朴素逐字检索、长历史优势功效不足（n=48 未过显著性）、与 Mem0 等价——**卖点只在情绪连续性这一个终点**。
  - 别学它的二阶 VAD 情绪动力学实现（开环反馈、单指数近似 bug），论文自己已证伪其响应价值；凌凌若要情绪惯性，用简单衰减即可。
  - 项目 4 个月没动静、单人节奏、生产存储未落地——**不宜作为运行时依赖**，只当设计参照。
- 来源链接：[GitHub zifacorp/zifamem](https://github.com/zifacorp/zifamem) · [arXiv 2607.17564 摘要页](https://arxiv.org/abs/2607.17564) · [arXiv HTML 全文（公式与实验细节）](https://arxiv.org/html/2607.17564v1) · [Hugging Face Papers 收录页](https://huggingface.co/papers/2607.17564) · [中文 README](https://github.com/zifacorp/zifamem/blob/main/docs/i18n/zh-CN.md) · 星标/活跃度：GitHub API `repos/zifacorp/zifamem`（2026-11-23 查询：17★ / pushed 2026-07-21 / license null）
