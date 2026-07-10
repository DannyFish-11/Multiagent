"""支付来源检查(PHASE3 M12 硬性笼子之一)。

支付动作永不允许被 M11 的邮件驱动或 M10 的网页内容触发——仅人类会话
(source=user)可发起支付链。这是与"审批级别"正交的一道独立闸门:
即便某笔支付金额低于 confirm 阈值(auto),只要来源不是人类会话也一律拒绝。
"""

from __future__ import annotations

from core.errors import LayerError

HUMAN_SOURCES = frozenset({"user"})


class PaymentSourceDenied(LayerError):
    def __init__(self, source: str) -> None:
        super().__init__(
            "L12", "payment-guard",
            f"支付链只能由人类会话发起,当前来源={source!r} 被拒绝"
            "(邮件驱动/网页内容永不允许触发支付)",
        )


def assert_human_initiated(source: str) -> None:
    if source not in HUMAN_SOURCES:
        raise PaymentSourceDenied(source)
