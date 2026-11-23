# PPet

- 仓库：https://github.com/zenghongtu/PPet · 星标：**2,033**（2026-11-22，GitHub API 实测） · 语言/栈：Electron + TypeScript（live2d-widget 内核魔改） · 许可：MIT · 活跃度：**停滞**（最后推送 2024-06-18，v1.2.1 停在 2020 时代，Issue 堆积；主表归入简要层合理）
- 一句话定位：Electron 桌宠先驱——把网页版 live2d-widget 塞进透明窗口 + 插件中心，是"Electron+Live2D"路线的开山参考，现已停止维护。
- 架构/功能要点：
  - Live2D 模型管理：**在线模型 URL 直接导入**（输入 raw.githubusercontent 的 model.json 地址即加载，免下载）+ 本地文件夹导入（要求含 model/textures/motions 三字段）；模型宽高手动设置 + "重新渲染"纠错按钮——反映出 live2d-widget 内核对模型规格的宽容度差。
  - 模型生态：README 直接导流 xiazeyu/live2d-widget-models（200+ 模型合集）、梦象等模型站；自身不提供模型（版权规避）。
  - 插件中心：默认插件如 take-a-rest（25 分钟休息提醒，做成插件而非内置）；插件开发有 wiki 指北。
  - PPet-Api：配套 HTTP 接口项目（供外部程序与桌宠交互）。
- UI/交互模式：
  - 交互极简：@置顶、忽略点击（点击穿透开关）、按住 Alt 拖动、Ctrl/Cmd + / − / 0 缩放、开机启动、调试面板。
  - 托盘菜单承载全部管理功能（模型切换/设置/插件开关/退出）；无养成数值、无商店——纯摆件+小工具路线。
  - 模特显示不完整时用户手动调 w/h——**没有做自动参数映射**（后来者 Live2DPet 的"自动扫描模型参数并映射眼球/头部追踪"就是对这个痛点的修复）。
- 对凌凌的启示：
  - **照抄**：①在线模型 URL 直载（用户贴一个 GitHub 链接就能试模型，分发成本归零——凌凌的模型管理器应支持"URL 预览→再决定导入本地"）；②"忽略点击"开关（桌宠与工作的边界控制）。
  - **改造**：插件中心思想用 dsh 插件体系实现（凌凌的插件是 AI 能力插件而非 take-a-rest 这类小工具）；把它没做的自动参数映射、模型规格校验、错误提示做成凌凌的模型导入向导。
  - **避开**：把休息提醒等功能做成插件的理由（"移到插件中心"）值得借鉴，但 PPet 的整体 UI 是 2020 年 Electron 原型水平（默认窗口感、无阴影调优、macOS 上 emoji 风图标）——凌凌的沉浸式无边框+毛玻璃路线远超它。
  - 反面教材价值：项目 2024 年停更后星标仍在涨（2,033），说明"Electron Live2D 桌宠"需求持续存在但**供给断档**——凌凌的窗口期。
- 来源链接：[GitHub README](https://github.com/zenghongtu/PPet)（存档 `_raw/ppet.md`）、[GitHub API](https://api.github.com/repos/zenghongtu/PPet)、[插件中心](https://github.com/zenghongtu/PPet/tree/master/plugins)、[live2d-widget-models](https://github.com/xiazeyu/live2d-widget-models)
