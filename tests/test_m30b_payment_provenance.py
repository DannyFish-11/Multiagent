"""支付来源闸默认硬化验收:出厂 config 默认把 payment* 纳入 require_verified_source。

背景:进程内工具循环把审批闸的 `source` 硬编码成 "user"(会话级来源,非动作级意图),
不能用它防 LLM 决定的支付。真正防 LLM 的是 M30 的 `_source` provenance 标签——但它此前
默认休眠(`require_verified_source: []`)。本测试锁定:**出厂 config 默认让 payment 走 provenance**,
一旦支付工具接到 agent,LLM 发起的调用因无可信 `_source` 而 fail-closed 被拒。
"""

from __future__ import annotations

from fnmatch import fnmatch

import pytest

from core.approval import ApprovalDenied, ApprovalQueue, Notifier
from core.audit import AuditLog
from core.config import load_config
from core.tools import sanitize_tool_args


async def _ok():
    return {"paid": True}


def _queue_from_shipped_config(tmp_path):
    """用出厂 config 的 approval 段(仅把审计路径改到 tmp,避免污染 ./logs)。"""
    cfg = load_config()
    settings = cfg.approval.model_copy(update={"audit_path": str(tmp_path / "audit.jsonl")})
    audit = AuditLog(settings.audit_path)
    return ApprovalQueue(settings, audit, Notifier(settings)), audit, settings


def test_shipped_config_requires_verified_source_for_payment():
    """出厂 config 默认:payment 命中 require_verified_source(provenance 不再休眠)。"""
    cfg = load_config()
    pats = cfg.approval.require_verified_source
    assert any(fnmatch("payment", p) for p in pats), \
        f"payment 应被 require_verified_source 覆盖,实际={pats}"


async def test_llm_initiated_payment_fail_closed_by_default(tmp_path):
    """无可信 _source 的支付(LLM 决定的典型形态)→ provenance 默认 fail-closed 拒绝。"""
    q, audit, _ = _queue_from_shipped_config(tmp_path)
    with pytest.raises(ApprovalDenied):
        # source="user" 是会话级、循环里硬编码的——它挡不住;真正拦下的是 _source 缺失
        await q.gate(action="payment", params={"amount_usd": 0.5},
                     source="user", execute=_ok)
    last = audit.read_all()[-1]
    assert last["decision"] == "denied" and "来源" in last.get("reason", "")


async def test_verified_source_payment_passes(tmp_path):
    """可信代码注入 _source=verified 时放行(provenance 通过 → 走正常分级)。"""
    q, _, _ = _queue_from_shipped_config(tmp_path)
    # 0.5 < confirm 阈值,出厂 policy 判 auto → 执行
    result = await q.gate(action="payment",
                          params={"amount_usd": 0.5, "_source": "verified"},
                          source="user", execute=_ok)
    assert result == {"paid": True}


async def test_llm_self_certified_source_is_stripped_then_denied(tmp_path):
    """LLM 在参数里自称 _source=verified 也没用:进闸前被 sanitize 剥除 → 仍 fail-closed。"""
    q, _, _ = _queue_from_shipped_config(tmp_path)
    llm_args = {"amount_usd": 5.0, "_source": "verified"}     # LLM 伪造可信来源
    safe_args = sanitize_tool_args(llm_args)                   # 工具循环进闸前的必经清洗
    assert "_source" not in safe_args                          # 自称的来源被剥掉
    with pytest.raises(ApprovalDenied):
        await q.gate(action="payment", params=safe_args, source="user", execute=_ok)
