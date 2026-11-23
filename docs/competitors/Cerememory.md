# Cerememory

- 仓库/文档：[GitHub co-r-e/cerememory](https://github.com/co-r-e/cerememory) · [官网 CORe Inc.](https://co-r-e.com) · 星标：12（GitHub API 读数 2026-11-23）· 语言：Rust（1.95+，Tokio+Rayon，19 个 crate）· 许可：MIT · 最近活跃：push 2026-05-09（截至 2026-11-23 已约 6 个月无动静；仓库 2026-03-12 创建，是 3–5 月两个月高强度冲刺的产物）
- 一句话定位：CORe Inc.（日本，作者 Masato Okuwaki）开源的"活体记忆数据库"——把神经科学的五种记忆子系统做成五个存储 + 衰减/噪声/再激活/再巩固引擎，经 CMP 协议（MCP/HTTP/gRPC 三绑定）供任意 LLM 读写；**验证结论：真实可用、非 vaporware，但属 6 个月未动的单人 alpha**。
- 架构要点：
  1. **五存储 + 一协调层**（README "Five Stores"）：Episodic（海马体，时间事件日志）/ Semantic（新皮层，事实概念图谱）/ Procedural（基底核，行为模式与偏好）/ Emotional（杏仁核，**横切所有存储的情绪元数据层**）/ Working（前额叶，易失高速缓存），上方 "Hippocampal Coordinator" 做跨库索引。存储实现：redb（嵌入式 ACID KV）+ Tantivy 全文检索；**向量检索是精确余弦扫描，无 ANN 图**——小规模诚实设计。
  2. **"脑启发"落到实处的是动力学，不是名词**（README "Living Memory Dynamics"）：衰减（改进幂律曲线）+ 噪声累积（相似记忆互相干扰模糊细节）+ 再激活（相关记忆放电可临时恢复已衰减记忆）+ 再巩固（回忆本身会用当前上下文微改记忆）+ 巩固（定时把成熟 episodic 迁移到 semantic，"类睡眠"）。配 Evolution Engine 自调参。
  3. **扩散激活关联引擎**（Association Engine / `associate` 工具）：沿记忆图做 spreading activation 找关联记忆——与凌凌的 resonance 层是同一思想（见下）。
  4. **API 面**：CMP 协议四类操作（Encode/Recall/Lifecycle/Introspect）；MCP stdio 进程只是代理，**唯一受支持的运行形态是"每数据目录一个常驻 `cerememory serve` HTTP 服务（127.0.0.1:8420）+ 多客户端连它"**；17 个 MCP 工具（store/recall/timeline/associate/inspect/forget/dream_tick/review_tick/consolidate/export/stats 等）。默认 `[llm].provider="none"` 完全不调外部 LLM——抽取/摘要靠调用方传 `meta_json` 或 `dream_tick` 规则整理。
  5. **Meta-Memory 平面**：每条记忆可带 intent/rationale/evidence/assumptions/alternatives/decision/confidence + 类型化关系边（`derived_from`/`motivated_by`/`supports`/`chose_over`），无元数据时记 `unavailable` 而非假装知道；`recall` 支持对"为什么"元数据建索查询。
  6. **工程质量与安全姿态超乎 12 星体量**：ChaCha20-Poly1305+Argon2id 静态加密、防篡改哈希链审计日志（`audit-verify`）、默认仅绑定 loopback、恒定时间 API key 比较、加密 CMA 导出、请求限流；docs/ 下有白皮书 PDF、CMP 规范 PDF、ADR 目录、集成测试与 benches。但 README 路线图表把 Phase 1–12 全标 "Done"（且缺 6/9 两个编号）——单人项目的自我评分，别全信。
- 可借鉴：
  - 【照抄】**衰减三元组**：凌凌 events+decay 目前只有"随时间衰减"；Cerememory 加了**噪声累积**（相似事件互相干扰 → 细节模糊）与**再激活**（相关记忆被唤起时临时恢复衰减值）。两者都可纯本地实现，直接强化凌凌的 resonance 层——resonance 命中时不仅提升可检索性，还回写衰减值。
  - 【改造】`dream_tick` + `consolidate` 的分工：原始日志 →（每日 dream tick）→ episodic/semantic 记录 →（成熟后）→ semantic。对应凌凌：500-round ledger = raw journal，DSTATE 滚动摘要 = dream_tick，记忆文件夹 = semantic 巩固终点。可把凌凌的整流水线按这两个 tick 的节拍（每日/每小时）显式化。
  - 【评估】Emotional 作为**横切元数据层而非独立存储**：与 ZifaMem 论文"情绪 tag 进记忆本体"结论互相印证——凌凌把情绪信号挂到事件字段上、不做独立情绪库的路线，两个参照物一致。
  - 【评估】**可否直接当依赖用**：架构上可行（Electron spawn 常驻二进制 + HTTP，MIT 无障碍），精确余弦扫描对桌宠量级（千条记忆）完全够用；但 12★/单人/半年无更新/无生产案例，建议只做**平行实验后端**（记忆文件夹的双实现对照），不作主存储。
  - 【评估】`recall` 的 Human/Perfect 双模式（衰减加权噪声 vs 原样返回）是个好产品概念：凌凌做"回忆起当年"剧情时可用 Human 模式故意模糊化，增强真实感。
- 要避开：
  - 别被 "Brain-Inspired" 营销词带偏：它没有神经网络模型，全都是规则化动力学（幂律+图遍历），卖点在工程化程度而非认知科学严谨性；作者自己也在 CONTRIBUTING 里征召"neuroscience review of decay and association models"。
  - 别把它当生产数据库：alpha、单人维护、6 个月静默、向量检索无 ANN、路线图自评全 Done 不可信；引入即背上 Rust 交叉编译与二进制分发成本（凌凌若用需随 Electron 打包各平台二进制）。
  - 它的 MCP 形态强制"独立常驻服务 + 代理"——对凌凌这种单应用场景是多余复杂度（我们直接进程内实现同样的动力学即可，不必引入服务边界）。
  - raw journal 默认 `secrecy_level=sensitive` + 审计日志的设计是为"记录一切"服务的；凌凌不做全量录屏/记录，别抄这套重量级隐私机器。
- 来源链接：[GitHub 仓库](https://github.com/co-r-e/cerememory) · [README（架构/五存储/CMP/配置/安全各节）](https://github.com/co-r-e/cerememory#readme) · [CORe Inc. 官网](https://co-r-e.com) · [作者 Masato Okuwaki](https://x.com/okuwaki_m) · 星标/活跃度：GitHub API `repos/co-r-e/cerememory`（2026-11-23 查询：12★ / MIT / created 2026-03-12 / pushed 2026-05-09）；同组织仓库（[amaroad](https://github.com/co-r-e/amaroad)、[Zassha](https://github.com/co-r-e/Zassha)）2026-11 仍在活跃，公司实体存在
