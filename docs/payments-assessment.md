# 支付能力评估(AP2 / x402)— 结论:预留接口,暂不实现

评估时间:2026-07(PHASE 2)。结论:本阶段不设支付 Milestone。

## 协议现状(2026 年中)

- AP2:Google 2025.9 发布,现由 FIDO Alliance 治理,v0.2;核心为三个签名
  Mandate(Intent / Cart / Payment,W3C 可验证凭证);作为 A2A 的扩展存在。
- 生产成熟度:crypto 路径(A2A x402 扩展,稳定币)production-ready;
  卡支付路径仍在与 Mastercard/PayPal 试点,未经大流量验证。

## 不现在做的三个理由

1. **本系统无购买场景**:当前用途(外脑/知识 agent/群体实验)没有一条用户旅程
   需要花钱。为能力而加能力违反 PHASE 1 非目标纪律。
2. **风险不对称**:支付是唯一"出错即真金白银损失"的能力,而治理层(Omnigent)
   与身份层(M5)都还年轻。AP2 官方定位明确:解决授权,不解决 agent 身份
   (身份归 Visa TAP 等补充协议)。先把 M5 身份做扎实。
3. **标准未收敛**:AP2 / ACP / x402 / MPP 多协议并存竞争,ACP 刚经历 OpenAI
   方向调整,现在深度绑定任何一家都可能押错。

## 已落地的预留(M5 交付)

- Agent Card 能力声明 schema 预留 `payments: []` 字段
  (`core/identity.py DEFAULT_PROFILE`、`adapters/a2a.py CardData`)。
- Omnigent 兜底策略:任何涉及资金/钱包/支付关键词的工具调用一律拒绝并告警
  (`omnigent/omnigent_policies/payments_guard.py`,默认拒付,未来显式解锁)。

## 复查触发条件(半年后,约 2027-01)

三条满足两条再立项:

1. 出现真实付费场景(如 agent 需自主购买 API 配额)
2. AP2 卡支付路径进入 GA
3. FIDO 身份工作组标准落地

首选试点路径:A2A x402 + 小额稳定币 + 单笔/日累计双限额。
