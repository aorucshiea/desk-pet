# live2d-automation MCP

- 仓库/文档：[GitHub J621111/live2d-automation](https://github.com/J621111/live2d-automation) · 星标：89（11 forks，GitHub API 读数 2026-11-23）· 语言：Python（≥3.10，fastmcp）· 许可：pyproject 与 README 声明 MIT，**但仓库内无 LICENSE 文件**（LICENSE/LICENSE.md/LICENSE.txt 均 404）· 最近活跃：最后 commit 2026-09-09（push 2026-09-27），2026-02 创建，约每月数次提交的中速节奏
- 一句话定位：从**单张角色立绘**生成"中间态 Live2D 包"的 MCP Server——9 步流水线（分析→人脸→分层→网格→绑定→物理→动作→导出）产出分层 PSD、绑定/物理/动作计划和一个 **mock moc3**；真正的 moc3 要靠它驱动 **Cubism Editor 的 Windows GUI 自动化**（PoC 级）来导出。
- 架构要点：
  1. **9 步 MCP 工具流水线**：`analyze_photo → detect_face_features → generate_layers → create_mesh → setup_rigging → configure_physics → generate_motions(idle/tap/move/emotional) → export_model → close_session`，每步 MCP 会话带 TTL、并发限制、输出目录锁死在 `output/` 下；输入限制：png/jpg/jpeg/webp、≤20 MiB、≤4096×4096（README "Step-by-step flow"）。
  2. **视觉栈可选装**：核心依赖只有 opencv/pillow/psd-tools/scipy/numpy；`[vision-cpu]` 加 mediapipe+rembg（抠图/人脸），`[vision-gpu]` 再加 torch+ultralytics(YOLO)+onnxruntime-gpu。远程语义部件检测默认关闭、上传需显式 opt-in（`LIVE2D_PART_API_ALLOW_UPLOAD=1`）+ 域名白名单——隐私设计值得注意。
  3. **导出是 mock，不是真模型（决定性事实）**：README "Export notes" 原文——"The exporter writes a **mock intermediate bundle**, not a production-ready Live2D runtime model"、"`ready_for_cubism_editor` remains **false** until a real Cubism-compatible exporter exists"。所谓 `.moc3` 只是满足文件契约的占位物，`model3.json` 引用它但**不能被 pixi/Cubism runtime 真正渲染**。
  4. **真导出路径 = GUI 自动化 Cubism Editor（PoC）**：`docs/native_gui_adapter_contract.md` 定义了外部适配器协议（`launch_editor / import_psd / apply_template / export_embedded_data` 四动作，exit 0=成功、64=回退）；内置 Windows 控制器用 PowerShell 脚本找窗口标题、模拟菜单键序（如 `[Modeling]→[Template]` 的 `%m t a`），且官方态度是**按键序必须针对每台机器的 Cubism 窗口手工校准**（`windows_cubism_default.json` 里 `template_menu_sequence` 故意留空），校准报告会告诉你哪些对话框恢复规则没匹配上。
  5. **分层是真的**：`generate_layers` 借 psd-tools 产出可继续使用的 PSD（README 提供"已有 Cubism-ready PSD、只想跑 Cubism 半场"的 `calibrate-template` 子命令）——即"立绘→分层 PSD"这一段是实打实工作的，"PSD→moc3"这一段不是。
  6. 无任何量化质量声明（无准确率/成功率数字），定位 Alpha（pyproject classifier `Development Status :: 3 - Alpha`）。
- 可借鉴：
  - 【评估·结论先行】**它不能直接解决凌凌"GIF 主题→Live2D"的内容瓶颈**：单图进来的产出是 mock moc3，Electron/pixi 拿到也渲染不出会动的角色。把它当"一键模型生成器"集成 = 白干。
  - 【改造】真正可借鉴的是它的**分层 PSD 生成段**（rembg 抠图 + 人脸关键点 → 分层输出）：凌凌若做 Live2D 化工具链，"图→分层 PSD"这半场可以参考它的 MCP 工具切分与输入约束（尺寸/格式/目录安全），省掉自研视觉栈选型时间。
  - 【评估】它的 **Cubism Editor GUI 自动化契约**（动作枚举 + exit code 语义 + 对话框恢复规则 + 校准报告）是目前公开资料里少见的"程序化驱动 Cubism Editor"参考。若我们决定走"AI 出 PSD + 人肉/半自动 Cubism 绑定"路线，这个契约可直接抄作内部工具设计。成本评估：PoC 级，每台机器需校准按键序，不可无人值守。
  - 【评估】MCP 会话管理（TTL、并发上限、显式 close、输出目录囚笼）是给凌凌任何长任务 MCP 工具的现成范本。【照抄】
- 要避开：
  - **别信"single image → full Live2D model"的字面承诺**——上游自己在导出说明里否认了它；以及别在其 LICENSE 落地前直接复制代码（声明 MIT 但仓库无 LICENSE 文件，法务上是"保留所有权利"状态）。
  - 别把 GUI 自动化当稳定依赖：窗口标题/菜单键序随 Cubism 版本漂移，上游 README 明说"错误的快捷键比没有更糟"、`apply_template` 失败是常态而非异常。
  - 邻近替代别漏看（均为 2026-11-23 GitHub 搜索 API 实查）：[tequed232/DSH-L2D](https://github.com/tequed232/DSH-L2D)（168★，JS，"DeepSeek Harness 工作区调用 MCP 接入 Live2D 模型"，偏运行时接入而非建模）；[nana7chi/CubismExternalEditMCP](https://github.com/nana7chi/CubismExternalEditMCP)（48★，走 Cubism Editor 5.4 alpha 的 External SDK 接口，比 GUI 键序模拟更正规）；[RevStudio/Rev2D](https://github.com/RevStudio/Rev2D)（46★，**放弃 Cubism 格式**、自定 JSON 绑定 + headless 渲染 + MCP，规避 Cubism 授权但失去 Live2D 生态模型）。
- 来源链接：[GitHub 仓库](https://github.com/J621111/live2d-automation) · [README（Export notes / Safety constraints / Native GUI adapter 各节）](https://github.com/J621111/live2d-automation#readme) · [Native GUI Adapter Contract](https://github.com/J621111/live2d-automation/blob/main/docs/native_gui_adapter_contract.md) · [pyproject.toml（依赖与 MIT 声明）](https://github.com/J621111/live2d-automation/blob/main/pyproject.toml) · [Cubism Editor 官方手册：应用模板](https://docs.live2d.com/en/cubism-editor-manual/applying-the-model-template/) · [官方手册：导出 moc3](https://docs.live2d.com/en/cubism-editor-manual/export-moc3-motion3-files/) · 星标/活跃度：GitHub API `repos/J621111/live2d-automation`（2026-11-23 查询：89★ / pushed 2026-09-27）
