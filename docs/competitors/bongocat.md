# BongoCat（ayangweb 版）

- 仓库：https://github.com/ayangweb/BongoCat · 星标：**24,744**（2026-11-22，GitHub API 实测；主表将其排低优先级严重低估，实际是本批第二高的项目） · 语言/栈：**纯 Rust**（不再是 Tauri——v2.x 迁移到 gpui-kit，即 Zed 编辑器的 GPUI 框架封装），23 个 workspace crates + 3 官方插件，Live2D 渲染为自研 crate，`unsafe_code = "forbid"`，LTO fat + strip，cargo-packager 打包 · 许可：Apache-2.0 · 活跃度：高（v2.4.1，最近推送 2026-10-28）
- 一句话定位：键鼠驱动的 Live2D 猫——你打字它打字、你点鼠标它拍爪、眼随鼠标，一个 10.5MB 安装包做到 Electron 桌宠 770MB 才有的流畅度。
- 架构/功能要点：
  - **"5MB"考证**：5MB 是 Steam Bongo Cat（C++ 原型，MMmmmoko/Bongo-Cat-Mver，1,146 星）时代的数据；ayangweb 版 v2.4.1 实测安装包：Windows exe **10.51MB**、macOS dmg 10.9–12.6MB（GitHub Release 资产实测）。即使翻倍，仍是 Electron 方案的 1/70。
  - 小包秘诀（Cargo.toml + crates 结构实证）：① 无 webview/无 Chromium——GPUI 原生 GPU 渲染；② `unsafe_code="forbid"` 全内存安全；③ 依赖精确锁版本（`=x.y.z` 全钉死）；④ 资源即模型包按需下载（model-store crate），不打包默认素材；⑤ 每个功能一个 crate（bongocat-input/live2d/render/audio/storage/update…），编译器做 tree-shake。
  - 事件管线：gilrs 手柄（自 fork 补丁）+ 键盘/鼠标全局监听 → bongocat-input 归一化 → Live2D 参数映射（眼睛/爪子/表情），支持游戏按键。
  - 插件系统：bongocat-plugin-sdk/manager/service/hud/packaging 五件套 + 官方三插件（input-source 输入法指示、key-sounds 打字音效、pomodoro 番茄钟）。
  - 完全离线运行、零数据收集（隐私作为卖点写进 README 首屏）。
- UI/交互模式：
  - 桌宠本体是"输入镜像"：每个按键/鼠标键/手柄键映射到猫的对应动作（不是随机反应，是 1:1 行为镜像——用户会盯着它确认"它真的在看我打字"）。
  - Live2D 眼球跟随鼠标 + 视线聚焦；模型热插拔（Awesome-BongoCat 社区模型仓库，用户自制模型分享生态）。
  - 窗口随意拖动、边缘吸附、点击穿透；托盘菜单 + 全局快捷键；HUD 插件（悬浮小组件）。
- 对凌凌的启示：
  - **照抄**：①**"行为镜像"交互范式**——凌凌现在靠截屏感知用户在干嘛，BongoCat 证明"直接监听输入事件"是零成本高确定性的感知源（我们已 hook 键鼠的话，把事件喂给情绪引擎比 VLM 看截屏便宜 100 倍）；②模型包按需下载的体积策略；③插件 SDK 五件套的划分（sdk/manager/service/hud/packaging）。
  - **改造**：Rust+GPUI 全重写对凌凌不现实（我们是 Electron+dsh 生态），但**渲染层可以外挂**——Live2D 渲染进程用 native 模块（如 pixi-webgpu 或 C++ addon）替代 DOM，能把凌凌从 770MB 拉回 <100MB 量级；key-sounds 插件（打字触发猫叫）是零成本的好感度外挂。
  - **避开**：自研渲染引擎的维护深渊（它 23 个 crates 是全职作者 AyangWeb 的产物）。
- 来源链接：[GitHub README](https://github.com/ayangweb/BongoCat)（存档 `_raw/bongocat.md`）、[Cargo.toml](https://github.com/ayangweb/BongoCat/blob/master/Cargo.toml)、[Releases v2.4.1](https://github.com/ayangweb/BongoCat/releases)、[GitHub API](https://api.github.com/repos/ayangweb/BongoCat)、[C++ 原型 Mver](https://github.com/MMmmmoko/Bongo-Cat-Mver)
