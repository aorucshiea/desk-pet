# 💎 SwarmWatch（看管编程 Agent 的桌宠）
- 出处：https://github.com/SwarmPack/SwarmWatch （Show HN: https://hn.svelte.dev/item/47244461 ） · 热度：HN 2 分小作 · 平台/栈：Tauri/Electron 常驻置顶 overlay + 本地 WebSocket + 每 Agent shim（Cursor/Claude/Cline/Copilot/VS Code）
- 一个词的特殊机制：宠物守门人 —— 宠物化身即 Agent 集群的状态本体，危险动作发生时宠物"卡住"并弹出审批
- 它怎么实现的：每个编程 Agent 一个小 shim，把工具调用/文件写入/shell 命令等事件经本地 runner 用 WebSocket 流进桌宠 app（全本地无遥测）。可把某些动作标记为 "guarded"（如 shell 命令、破坏性文件操作）：Agent 撞上守卫动作时 runner 暂停，化身翻转为 "blocked/needs you" 状态并弹出审批面板，一键批准/拒绝后 runner 立即继续或中止；全程有透明审计轨迹。
- 为什么有趣：技术/生命感 —— "宠物脸"成为权限系统的界面：宠物的表情状态就是系统的安全状态，审批像安抚一只受惊的动物而非点对话框。
- 对凌凌的启示：改造 —— 凌凌若承接工具执行（搜索/写文件），给刚直觉加"守卫动作"清单：撞上即冻结 + 变身"求批准"形态，等主人点头才继续；把"blocked"做成一个宠物动画状态而非系统弹窗。
- 可验证性：高
