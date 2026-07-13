# 支付能力评估(AP2 / x402)— 已于 PHASE3 M12 实现(默认拒付,带硬性笼子)

> **状态更新**:本文最初是 PHASE 2 的"暂不实现"评估;**支付能力已在 PHASE3 M12 落地**
> (`adapters/payments.py` + `core/payment_guard.py`),带单笔/日/月三层硬性笼子 + 仅人类会话
> 可发起的来源闸。下方 PHASE 2 的评估内容作为**决策存档**保留,不再代表当前状态。

## 落地形态(PHASE3 M12)

- **执行**:`adapters/payments.py::PaymentsAdapter.pay(*, amount_usd, payee, purpose, source)`
  —— virtual_card(首选,单次限额用完即焚)/ x402(补充);供应商由人类选定开户(停点)。
- **硬性笼子(缺一不上线,全 config 化,`core/config.py::PaymentsSettings`)**:
  单笔 `per_tx_usd` / 日 `daily_usd` / 月 `monthly_usd` 三层独立计数 + `confirm_threshold_usd`
  + 可选商户白名单(`whitelist_enabled`/`payee_whitelist`)。
- **来源闸**:`core/payment_guard.py::assert_human_initiated`——支付链仅人类会话(`source="user"`)
  可发起;`pay()` 的 `source` **必填且内部强制**,邮件驱动/网页内容永不允许触发(即便金额低于阈值)。
- **原子预留**:`PaymentLedger` 的 `reserve→charge→finalize`(结算失败自动 `refund`),在同一把锁内
  校验限额并占位,闭并发多笔的 check→charge TOCTOU 超支窗口。
- **AP2 留形**:审计记录 Intent / Cart 双结构,未来平移到 AP2 Mandate。
- **默认拒付**:`payments.enabled=false`(默认)时维持附录 A 拒付,须人类显式开启并配好供应商方可上线。

验收见 `tests/test_m12_payments.py`(全 mock,不花真钱)。

---

## 附:PHASE 2 历史评估(决策存档,已被 M12 覆盖)

评估时间:2026-07(PHASE 2)。当时结论:本阶段不设支付 Milestone。以下论证仅供了解决策脉络。

### 协议现状(2026 年中)

- AP2:Google 2025.9 发布,现由 FIDO Alliance 治理,v0.2;核心为三个签名
  Mandate(Intent / Cart / Payment,W3C 可验证凭证);作为 A2A 的扩展存在。
- 生产成熟度:crypto 路径(A2A x402 扩展,稳定币)production-ready;
  卡支付路径仍在与 Mastercard/PayPal 试点,未经大流量验证。

### 当时"不现在做"的三个理由

1. **本系统无购买场景**:当时用途(外脑/知识 agent/群体实验)没有一条用户旅程需要花钱。
2. **风险不对称**:支付是唯一"出错即真金白银损失"的能力,而治理层与身份层(M5)都还年轻。
3. **标准未收敛**:AP2 / ACP / x402 / MPP 多协议并存竞争,深度绑定任何一家都可能押错。

> 后续进展:M12 采取的路线正是当时预判的"首选试点路径"——x402/虚拟卡 + 小额 + 多层限额,
> 并在其上加了**人类来源闸**(唯一可发起方 `source="user"`)与**原子预留**两道当时未展开的控制。
> 深度绑定风险由 config 化的供应商选择 + 默认拒付缓解:不启用即零暴露。

### 已落地的预留(M5 交付,仍有效)

- Agent Card 能力声明 schema 预留 `payments: []` 字段
  (`core/identity.py DEFAULT_PROFILE`、`adapters/a2a.py CardData`)。
- Omnigent 兜底策略:任何涉及资金/钱包/支付关键词的工具调用一律拒绝并告警
  (`omnigent/omnigent_policies/payments_guard.py`,Omnigent 形态下的默认拒付层)。
