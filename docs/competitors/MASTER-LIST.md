# 竞品调研总表（2026-11-23，用户提供；状态由 lead 合并维护）

> 状态跟踪：⬜ 未调查 / 🔄 调查中 / ✅ 研究卡完成
> 研究卡目录：`docs/competitors/<项目名>.md`；原始资料存档 `docs/competitors/_raw/`
> 优先级原则：星标高的先查；深度分析层（多轮讨论过的）视为已知重点；机制猎奇层不看星标。

## 深度分析层（多轮讨论）

| 项目 | 开发者 | 核心特点 | 状态 |
|---|---|---|---|
| Alife | BDFFZI | C#/.NET 赛博生命框架，全插件化，AI 自己写插件，永久唯一会话，日均不到 2 元 | ✅ alife.md |
| AAAAGENT | phoiex | macOS 优先 Live2D 陪伴助手，任务转交给 DeepSeek Harness/Codex | ✅ aaagent.md |
| Coopanion | Pal-AI-Lab | 桌宠 Coo+电脑操作，四级权限分档，DeepSeek 大肥鱼分件动画 | ✅ coopanion.md |
| Shinsekai | RachelForster | Galgame/剧情向 RPG 助手，情绪标签→立绘联动，100+ 角色包生态 | ✅ shinsekai.md |
| FaustBot | liwusen | AI 自进化（Agile Engine），三合一记忆+Araya 维护，Gesellschaft AI 社区 | ✅ faustbot.md |
| 啾糯桌宠 | nicedoctors | WPF 桌面物理系统，窗口碰撞/任务栏行走/回收站进食 | ✅ jiunuo-desktop-pet.md |
| MiniCPM Desk Pet | OpenBMB | 本地优先，MiniCPM5-0.9B 驱动，感知编程 Agent 状态 | ✅ minicpm-desk-pet.md |
| clawd-on-desk | rullerzhou-afk | 像素桌宠监控 AI Agent 状态（本项目直接上游） | ⬜（自家历史，低优先） |

## 中度分析层

| 项目 | 核心特点 | 状态 | 调研纠错 |
|---|---|---|---|
| AI Desktop Pet（Steam ¥52） | 42kami，本地 LLM+酒馆角色扮演+音色克隆，Live2D/VRM/Spine | ⬜ 闭源仅功能参照 | — |
| VPet Simulator | LB Game，投喂/学习/打工/好感度养成，200+ 动画 | ✅ vpet.md | 星标实测 7,273；数值系统已从 GameSave.cs 逆向 |
| Project AIRI | Neuro-sama 复刻，Live2D/VRM+DuckDB+Minecraft | ✅ airi.md | **星标实测 51,649**（远超预估）——赛道头牌 |
| HermesPet | macOS 刘海桌宠 | ✅ hermespet.md | ⚠️ **无开源许可**（©All rights reserved），主表误记 Apache-2.0；仅可看产品形态不可抄代码 |
| Live2DPet（x380kkm） | Electron Live2D，屏幕感知+VOICEVOX+关键帧视觉记忆 | ✅ live2dpet.md | 星 93 但与 capture_screen/台账直接对标；其记忆池已被作者弃置=教训 |
| OpenPets | Plugin SDK v3 沙盒，宠物图鉴 | ⬜ | — |
| Bongo Cat | 键鼠计数+帽子掉落 | ✅ bongocat.md | **实测 24,744 星；实为纯 Rust + GPUI（Zed 框架），安装包 10.5MB**——反膨胀标杆 |
| MateEngine | VRM 3D，音乐律动，头眼追踪 | ⬜ | — |
| Replika | 商业 AI 伴侣 | ⬜（非开源，形态参照） | — |

## 简要层

Shimeji / Desktop Goose / Pets Therapy / Wallpets / eSheep / Neko / 兽耳助手 / 萌灵桌宠 / Deskmon / Kizuna Desktop / DeepSeek-DesktopPet / Desktop Pet (RPGames) / 星尘 Stardust / 艾伴助手 Ipet / 璇玑·貂蝉 — ⬜（低优先，机制猎奇由 gem 卡覆盖）

已单独出卡：
- DyberPet ⬜→✅ dyberpet.md（**实测 1,030 星；仓库 ChaozhongLiu/DyberPet；⚠️ GPL-3.0**——抄架构可以，抄代码会传染许可）
- BongoCat(ayangweb) ✅ 见上
- vscode-pets ✅ vscode-pets.md（4,200 星）

## 技术组件（直接影响架构）

| 名称 | 用途 | 状态 |
|---|---|---|
| **Laya** | 开源 System One 决策模型（4.2 亿参数，Apache 2.0）——laya_sidecar 跑的就是它 | ✅ Laya.md |
| ZifaMem | 情绪长期记忆框架（平行演化对象） | ✅ ZifaMem.md |
| **live2d-automation MCP** | 单图 → 完整 Live2D 生成 pipeline（内容瓶颈钥匙） | ✅ live2d-automation-mcp.md（含 mock-moc3 披露） |
| pixi-live2d-display | Web 端 Live2D 渲染标准 | ✅ pixi-live2d-display.md（Cubism Expandable-Application 许可限制） |
| Cerememory | 脑启发记忆数据库（Rust） | ✅ Cerememory.md |
| Jev | TypeSafe 闭源决策模型 | ⬜ 闭源，低优先 |
| VTube Studio API | Live2D 参数注入 WebSocket 协议 | ⬜（用户裁定收兵，未开始） |
| DeepSeek Harness (dsh) | 一切皆插件的 Agent 框架（我们运行于其上） | ⬜（自家环境） |

## 机制猎奇层（不看星标，只看特殊度）

| 内容 | 状态 |
|---|---|
| gem-*.md 卡 + `_gems.md` 排名 | 🔄 research-gems2 |

## 已完成（本轮之前）

| 项目 | 产出 |
|---|---|
| Open-LLM-VTuber | ui-roadmap-2026-11.md 矩阵 + 已克隆 E:\deskpet-references\open-llm-vtuber |
| dsh-chatting-pet | 同上 + 已克隆（Edge 免费 TTS / 角色卡导入模式） |
| Desk-Pet (Rust) | 同上 + 已克隆（记忆基准 LongMemEval 96.2%） |
| AI Desktop Pet (Steam) | 闭源功能参照（VRM/音色克隆/Workshop） |
