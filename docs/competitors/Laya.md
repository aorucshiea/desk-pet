# Laya

- 仓库/文档：[GitHub NandhaKishorM/laya](https://github.com/NandhaKishorM/laya) · [文档站](https://nandhakishorm.github.io/laya/) · [PyPI](https://pypi.org/project/laya/) · [HF convaiinnovations/laya](https://huggingface.co/convaiinnovations/laya) · 星标：35,759（3,206 forks，ungh 镜像 2026-11-23 读数）· 语言：Python（另发 TypeScript SDK：`laya-ts` 本地推理 / `laya-client` HTTP 客户端）· 许可：Apache 2.0 · 最近活跃：2026-11-21 push（≈2 天前，极高频；仓库 2026-09-18 创建，9 周冲到 3.6 万星）
- 一句话定位：开源非自回归 System 1 决策引擎——对任意结构化 state 在**一次前向**里回答 choice/score/noul 三类 typed 问题并输出校准概率，是闭源 Jev（TypeSafe）的开源对位；我们 `laya_sidecar` 跑的就是它（`laya[onnx,serve]==0.4.15`）。
- 架构要点：
  1. **模型是什么**：ModernBERT 家族编码器（~150M–421M），不生成文本；用 RLCD（强化学习 + strictly proper scoring rules）训练使概率可校准。T4 上 ~33ms/决策、batch 后 ~16ms。三个官方 checkpoint：`laya`（英文 421M，512 ctx）、`laya-multilingual`（100+ 语言，1024 ctx、可 `max_len=8192`）、`laya-typed-decisions`（typed 决策工作流微调）。**上游没有 "zh-tier"**——那是我们 `start.ps1/start.sh` 用 `LAYA_EXTRA_MODELS` 把本地 `laya-multilingual` checkpoint 注册成的别名（本地文件 `pet-sidecar/laya_sidecar/start.ps1:336-342` 已证实）。
  2. **/v1/systemone 契约**：与 Jev 逐字段兼容的 wire 协议（`laya.serve`，FastAPI+uvicorn）。请求 `state + questions{type:choice|score|noul, instructions, criteria}`；响应 `choice/score/noul + probabilities + confidence(1−归一化熵) + answer_confidence(max p) + usage{input_tokens,output_tokens}`。另有 `/v1/systemone/batch`（≤64 states、按序返回 + `total_usage`）、`/models`、`/health`。逐 call 控制项：`model/task/lang/lang_guess/min_confidence/max_len/head_max_len`；五个 hook 参数在 HTTP 层一律 422（hook 必须住在服务端进程）。
  3. **服务端护栏**：choice 选项 ≤100（超限 413）；选项共享 `head_max_len` 预算（英文 192 / 其他 256 token），超预算裁剪、完全不 fit 才 422；超长 state 截断并在 usage 回报 `truncated/state_tokens_dropped`（`LAYA_JEV_STRICT=1` 隐藏）。运维环境变量：`LAYA_HOST/PORT/DEVICE/PRELOAD/MODELS/THREADS/MAX_LOADED/IDLE_UNLOAD_SECONDS/EXTRA_MODELS/API_KEY`。
  4. **0.5 换代 vega 引擎**（frozen 0.8B reader + "能量谷"物理头）：73,728 token 上下文、image/audio/video 输入、`abstain`/`unbound`/conformal prediction set/自动 `route` 门槛、`Learner/fit()` 用确认答案秒级拟每问题逻辑头（无需微调）。学习型基准：banking77 4,928 行后 87.5%（ECE 0.028）；14 决策模板 120 确认后 96.0% vs Jev 92.6%。经典 encoder 路径保留（`vegaml=False`）。
  5. **意图与生态**：定位就是"分类/路由/门控"（<20 选项甜点区），不是生成；extras 覆盖 `onnx|serve|mcp|fast(TileLang)|structured|langchain|vision|audio`；社区 laya-mlx 把它搬上 MLX（M3 Max 贪吃蛇 60 决策/s、~1GB 内存）；arXiv 2610.33775 将其列为 System 1 模型代表（§6.3）。
- 可借鉴：
  - 【照抄】上游明确警告：**两个 checkpoint 出厂即过自信、`laya-multilingual` 完全没有拟合温度**，且 confidence 随选项数漂移（英文 3 选项均值 0.77 / 20 选项 0.97，multilingual 方向相反）。对策照抄：按 `choice:3-5/6-10/11+/default` 分桶拟合 `min_confidence` 门槛（`fit_abstention_thresholds`），答案回显 `abstention: passed|abstained|unevaluated`。我们 sampler 的指数混合应改为先过自己的温度拟合，再信概率。
  - 【照抄】"Gate on route, never on confidence"：置信数字只排序、不建立正确性——写进凌凌决策层注释与验收标准。
  - 【改造】vega 的 `fit(rows)` 流程（frozen 特征上每问题拟 logistic 头 + 温度/conformal 校准）：凌凌决策日志正好是 rows 来源，攒标签后给 `thinking` 问题拟专属头；`LAYA_EXTRA_MODELS` 注册模式也支持将来挂"凌凌-tier"微调 checkpoint。
  - 【评估】`predict_shortlist`（多选项先缩表）、`predict_long`（窗口扫描 + span 归因）可改善我们的 state 组织；`laya-ts` 浏览器端 ONNX 推理可评估是否砍掉 Python sidecar（体积 vs 架构简化）。
  - 【符合意图判定】我们的用法（thinking 预算 4 选项 choice → 概率喂 sampler）完全落在作者意图内，0.4.15 的 torch 路径与上游 serve 契约一致，无协议漂移；上游 0.4.15 明确不含 torch-free ONNX 服务路径（本地 smoke 已验证），与官方架构说明一致。
- 要避开：
  - 别把升级 0.5 当小事：默认引擎换成 vega（0.8B reader），CPU 延迟/内存/依赖全变，`abstain/route` 会改变 sampler 输入分布；要升就固定版本并重跑 smoke。
  - 别在一个共享 state 里塞多条记录问 `rows[i]` 类问题（上游明确：一请求一 state；按记录走 batch）。
  - 别指望 HTTP 层执行 hook/审计（422 拒绝）；日志与审计留在 gateway 侧。
  - 别用熵版 `confidence` 做门槛——用 `answer_confidence`（max p，温度拟合的对象）。
- 来源链接：[GitHub](https://github.com/NandhaKishorM/laya) · [PyPI laya](https://pypi.org/project/laya/) · [HF convaiinnovations/laya](https://huggingface.co/convaiinnovations/laya) · [HF convaiinnovations/laya-multilingual](https://huggingface.co/convaiinnovations/laya-multilingual) · [arXiv 2610.33775 §6.3](https://arxiv.org/html/2610.33775) · [163：Laya vs Jev 社区实测](https://m.163.com/dy/article/L7GM47F40511AQHO.html) · 本地佐证：`pet-sidecar/laya_sidecar/README.md`、`pet-sidecar/gateway/decision/laya_router.py`
