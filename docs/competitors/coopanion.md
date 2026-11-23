# Coopanion（Pal-AI-Lab）

- 仓库/主页：https://github.com/Pal-AI-Lab/Coopanion · 星标：3726（fork 149，本批最高）· TypeScript/Electron（组装自自家 Cortico 框架：Cortico Core + Cormini Persona + desktop-pet World）· 许可：AGPL-3.0-or-later（≤0.1.10 为 MIT；Cortico 框架 MIT）· 活跃度：2026-09-23 建库，最后 push 2026-11-22（当日），48 open issues，三平台安装包 + 扩展市场，迭代极快
- 一句话定位：屏幕底边行走的 AI 桌宠 Coo——可以聊天陪伴，也可以在你授权后替你操作整台电脑；四级权限分档 + 沙箱形象包（Live2D 式分件动画）+ World 扩展生态，是"桌宠 = 可信的电脑操作员"路线的标杆。
- 架构要点（四级权限模型 + 分件动画，中等优先级 5）：
  - **CUA 四级权限（源码验证）**：`cua.permission` 从严到松四档——`ask-each-turn`（每轮第一次截图/输入前都问）/ `ask-before-acting`（看屏幕不问、动手前问）/ `ask-once`（默认，动手前问一次，同意后 30 分钟内不再问）/ `never-ask`。另有独立加问项：向终端打字或按 Win+R 前必须单独再问一次。人机接管：用户一碰鼠标键盘，Coo 立即暂停操作；每次唤醒限 40 次模型请求；拒绝/超时（气泡 60 秒）后本轮所有待批调用不执行。宠物不在时降级为系统对话框提问。
  - **自主配置权限（bot 改自己设置）**：`selfAdjust` 也分四档——default（形象/打扮/走动直接改，音效/大小/夜间模式先问）/ any / custom（逐项 true/false）/ off；气泡三按钮"可以/以后都可以/不用了"，选"以后都可以"自动转 custom 并记录白名单——**一次授权逐项积累**的同意记忆机制。
  - **分件动画 = 沙箱形象包**：身体全部来自"形象包"目录（`figure.json` 清单：axes 打扮维度 / vocab 动作词表 / sounds / moves 自定义移动目标 / bubble 气泡配色）。包代码在**不透明源 CSP 沙箱 frame** 里跑：网络只许 fetch 本包目录、无 socket、与宿主仅 postMessage；身体报告的框、命中圆、特效范围都被宿主钳制。DeepSeek 大肥鱼是"Live2D 式分件模型"（`web/rig` 画的分层部件 + 8 套配色），也支持直接加载真 Live2D `.moc3`。包抛错/20 秒未就绪自动回退 Coo。**兼容承诺**：figure.json + 沙箱契约 + kit API 三件套对外冻结、契约测试锁定（`tests/figure-frame-contract.test.js`）。
  - **窗口感知（和物理沾边）**：`needs:["windows"]` 的包每 0.7 秒收到一次桌面顶层窗口的舞台坐标（Windows 用 `GetTopWindow` z 序 + `DwmGetWindowAttribute` 可见边框，不读标题），提供 `ledges`（可见顶边列表，按 z 序裁剪遮挡——**窗口窗沿可以作为行走平台**）和 `windows`（完整矩形 + 前台标记）。
  - **其他**：14 家模型服务商 + ChatGPT 账号登录；本地 ASR（FunASR/Whisper）；记忆写 `PARTNER.md` + 按主题日期组织 + Git 版本化每次改动；World 扩展市场（QQ 机器人、画室、小游戏）；OBS 透明直播输出；按帧截图实现的 overlay。
- UI/交互模式：屏幕底边行走 + 悬停按钮轮盘 + 气泡对话（气泡可固定 1–60 秒）；点击穿透模式（打游戏时点击穿过宠物）；Alt 按住说话/双击 Ctrl 打字；首启引导全部走气泡对话而非表单。
- 对凌凌的启示：
  - **照抄**：①四级权限分档 + "看屏幕/动手"分离 + 高危动作（终端/Win+R）单独加问——直接套用到我们 screen-consent 流程设计，特别是 `ask-once` + 30 分钟授权期的人性化默认；②"以后都可以"逐项累积授权（同意记忆）；③形象包沙箱契约 + 冻结承诺 + 契约测试——我们未来开放皮肤/器官包生态时的安全模板；④`ledges` 窗沿行走平台数据源（每 0.7s z 序裁剪遮挡）给我们的 rigid-body 世界一个更轻的窗口感知方案备选。
  - **改造**：分件动画（web/rig 分层部件）比我们 GIF 帧动画上限高（配件/配色可组合、动作词表可数据驱动），但比 Live2D 工程量小——可作为凌凌形象系统的中间路线选项；其 `vocab`（动作词表即 LLM 词表，环境提示词渲染成表格）模式可改造进我们的 [EMOTION:] 动作协议。
  - **避开**：①CUA 全键盘鼠标接管路线（macOS/Win/Linux 三平台权限维护极重，且与凌凌"看护编码 Agent"定位不同）；②3726 星但 149 fork 的比例说明用户远多于贡献者，Electron 巨型应用路线成本可见——不跟。
- 来源链接：
  - [仓库 README](https://github.com/Pal-AI-Lab/Coopanion)
  - [desktop-pet World 完整文档（形象包/权限/窗口感知源码级细节）](https://github.com/Pal-AI-Lab/Coopanion/blob/main/packages/cortico-world-desktop-pet/README.md)
