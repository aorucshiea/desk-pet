# 💎 zerogochi（链上判决电子宠物）
- 出处：https://ethglobal.com/showcase/zerogochi-vwyip （源码：https://github.com/LevanIlashvili/zerogochi ） · 热度：ETHGlobal "Open Agents" 参赛作品 · 平台/栈：Telegram Mini App (Next.js14) + NestJS + 4 份 Solidity 合约（0G 主网）+ DeepSeek in TEE
- 一个词的特殊机制：JUDGE 按钮 —— 宠物拉取自己全部链上历史（每笔喂食/玩耍/对话都是真交易），生成一份引用具体事实的"对主人的判决书"
- 它怎么实现的：每只宠物是 iNFT（ERC-7857），人格由 8 轴向量（anxious/dramatic/affectionate/talkative/cynical/curious/vain/loyal）客户端生成、AES-GCM 加密上 0G Storage、哈希上链。所有互动（feed/play/chat）均为链上交易（EIP-2771 代付 gas）；JUDGE 时把全部 Born/Fed/Played/Spoke 事件组成编年史交给 TEE 内的 DeepSeek 渲染判决，引用真实时刻（"你两小时前铸造了我只喂了两次"）。另有 DREAM 按钮（以扭曲真实历史的方式叙述超现实梦）、60-120 秒一次的自主自言自语（真链上推理结算）、被转移新主人时行为随之改变、幼年→成年→老年（sprite 缩放）、永久死亡（饥饿归零超 24h 合约标记死亡，无复活）。
- 为什么有趣：情绪/生命感 —— 记忆被做成"不可篡改的事实账本"，宠物的怨气/感激因此有了证据力；DREAM 则证明"梦=历史的扭曲重放"可被产品化。
- 对凌凌的启示：改造 —— 不上链，但给台账加"判官器官"：凌凌可随时被要求"评价主人最近一周"，必须引用台账里的具体事件句（不可凭空），输出带立场的小判决；长饥饿→永久死亡也可借其严肃性。
- 可验证性：高
