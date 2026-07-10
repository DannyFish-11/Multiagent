"""记忆上交策略(M5.3):决定一条私有记忆是否值得进入共享池。

PromotionPolicy 为协议;交付 GraderPolicy(LLM 评分)与 ManualPolicy(人工确认)。
第三个实现位(群体投票 VotePolicy)为后续"群体投票 vs grader"对比实验预留,
本阶段不实现(附录 B:N>2 群体实验属 PHASE 3)。
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from core.errors import LayerError
from core.schemas import Message

if TYPE_CHECKING:
    from adapters.llm import LLMClient

Decision = Literal["promote", "reject", "pending"]

GRADER_SYSTEM = """\
你是记忆价值评估器。判断给定记忆是否值得进入多个 agent 共享的公共记忆池。
值得共享:客观事实、可复用的知识、通用偏好模式。
不值得:高度私人化信息、一次性琐事、隐私敏感内容。
输出严格 JSON:{"score": 0.0到1.0, "reason": "一句话理由"}。不要输出其他字符。"""


@runtime_checkable
class PromotionPolicy(Protocol):
    async def decide(self, content: str, meta: dict) -> Decision: ...


class GraderPolicy:
    """LLM 评分 >= 阈值 → promote。"""

    def __init__(self, llm: "LLMClient", threshold: float = 0.7) -> None:
        self._llm = llm
        self._threshold = threshold
        self.last_reason: str = ""

    async def decide(self, content: str, meta: dict) -> Decision:
        raw = await self._llm.chat(
            [Message(role="system", content=GRADER_SYSTEM),
             Message(role="user", content=content)],
            temperature=0.0,
        )
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            raise LayerError("L2", "promotion-grader", f"评分输出非 JSON: {raw[:200]}")
        try:
            data = json.loads(m.group(0))
            score = float(data["score"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise LayerError("L2", "promotion-grader", f"评分解析失败: {raw[:200]}") from exc
        self.last_reason = str(data.get("reason", ""))
        return "promote" if score >= self._threshold else "reject"


class ManualPolicy:
    """人工确认:一律 pending,进入待审队列;approve/reject 由人显式调用。"""

    def __init__(self) -> None:
        self.queue: list[dict] = []

    async def decide(self, content: str, meta: dict) -> Decision:
        self.queue.append({"content": content, "meta": meta})
        return "pending"


# 实验位预留:class VotePolicy —— 群体投票上交(PHASE 3,见 PHASE2_SPEC 5.3)
