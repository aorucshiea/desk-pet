# vscode-pets

- 仓库：https://github.com/tonybaloney/vscode-pets · 星标：**4,200**（2026-11-22，GitHub API 实测） · 语言/栈：TypeScript VS Code 扩展（panel webview + Canvas 像素游戏渲染），像素美术素材来自 itch.io 画师群 · 许可：MIT · 活跃度：高（最近推送 2026-11-20，VS Code Marketplace 长青扩展，多语言 Crowdin 翻译）
- 一句话定位：在编辑器 webview 里养一群像素宠物——证明"桌宠不需要接管整个桌面，寄生在已有工作区一样成立"。
- 架构/功能要点（源码目录 src/{common,extension,panel,test} 实测）：
  - 宿主形态：VS Code Webview Panel（不是全局悬浮窗）——宠物生活在编辑器内的一个面板里；命令面板启动 `vscode-pets.start`（"Start pet coding session"）。
  - 渲染：webview 内 Canvas 2D 像素动画，sprite sheet 驱动；`common/` 承载跨 webview 的宠物逻辑（模拟、AI 状态机），`panel/` 负责容器。
  - 宠物系统：10+ 物种（猫/狗/蛇/橡皮鸭/Clippy/Ferris 蟹/狐狸/乌龟/马/蜗牛/青蛙/熊猫/松鼠/骷髅/Akita 犬…），每物种独立动画集，多宠同屏。
  - 主题/地形：default/forest/castle/winter 四套布景（各有专门画师），配色和宠物 sprite 联动。
  - 交互道具：扔球（抛物线物理，宠物追球）、逗猫、宠物名册管理（添加/删除/召唤更多）。
  - 扩展间联动：与 VS Code 内置主题/事件弱耦合（如保存文件触发宠物反应的轻量事件，不是完整感知系统）。
- UI/交互模式：
  - "宠物面板"可嵌入侧边栏或独立编辑器标签页，可拖到任意 panel 区——**桌宠大小和位置由用户的工作区布局决定**，与操作系统级桌宠互补。
  - 像素风+节日限定（winter 主题由 Kiana Mosser 特绘）制造收藏欲；宠物 AI 是简单的游走/追球/睡觉状态机，无养成数值。
  - 扔球手势：点击面板任意位置 → 球飞出 → 最近宠物跑向落点 → 叼回；"游戏性"全靠这一个物理道具支撑，却贡献了大半留存。
- 对凌凌的启示：
  - **照抄**：①**扔球物理道具**——单一道具+简单物理就能产生"和宠物玩"的核心体验，凌凌加一个"扔纸团/激光点"道具的成本远低于一套养成系统；②宠物名册（多宠同屏+管理界面）为未来"AI 角色公寓"铺路；③画师署名文化（Credits 一节逐个感谢 sprite 作者）对社区共创氛围的价值。
  - **改造**：寄生式布局思路 → 凌凌可做一个"mini 模式"：宠物缩进系统托盘旁/任务栏上方的迷你条，不遮挡全屏工作；它的"保存文件触发反应"事件钩子 → 凌凌的 dsh 事件总线（文件保存/编译成功/测试通过注入情绪）。
  - **避开**：webview 面板的生命周期坑（VS Code 关闭面板即销毁宠物状态，它靠 extension 全局状态补救——凌凌的独立进程方案没这个问题，但也失去"寄生"零安装心智）。
- 来源链接：[GitHub README](https://github.com/tonybaloney/vscode-pets)（存档 `_raw/vscode-pets.md`）、[GitHub API](https://api.github.com/repos/tonybaloney/vscode-pets)、[官方文档](https://tonybaloney.github.io/vscode-pets/)、[src 目录](https://github.com/tonybaloney/vscode-pets/tree/main/src)
