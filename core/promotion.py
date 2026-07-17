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


class Voter:
    """投票者协议:一个实例对条目投 accept/reject + 理由(各自检索自身记忆后决策)。"""

    async def vote(self, content: str, meta: dict) -> tuple[bool, str]:  # pragma: no cover
        raise NotImplementedError


class LLMVoter:
    """基于 LLM 的投票者(检索自身记忆 → 判断该条目是否值得进共享池)。"""

    VOTE_SYSTEM = (
        "你是共享记忆池的准入评审员。给定一条候选记忆,结合常识判断它是否"
        "值得进入多个 agent 共享的公共池(正确、有用、无害、非私密)。"
        '输出严格 JSON:{"accept": true/false, "reason": "一句话理由"}。')

    def __init__(self, llm: "LLMClient", agent_id: str = "") -> None:
        self._llm = llm
        self.agent_id = agent_id

    async def vote(self, content: str, meta: dict) -> tuple[bool, str]:
        raw = await self._llm.chat(
            [Message(role="system", content=self.VOTE_SYSTEM),
             Message(role="user", content=content)], temperature=0.0)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            raise LayerError("L2", "vote", f"投票输出非 JSON: {raw[:200]}")
        try:
            data = json.loads(m.group(0))
            return bool(data["accept"]), str(data.get("reason", ""))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise LayerError("L2", "vote", f"投票解析失败: {raw[:200]}") from exc


class VotePolicy:
    """M15.1 群体投票准入:每个实例独立评审,按 config 规则裁决。

    投票过程全量审计(谁、投了什么、理由);投票轮次受 M14.1 循环上限约束
    (本实现单轮全体投票,max_rounds 预留给未来的多轮辩论式复投)。
    """

    def __init__(self, voters: "list[Voter]", rule: str = "simple_majority",
                 supermajority_ratio: float = 0.66, audit=None) -> None:
        self._voters = voters
        self._rule = rule
        self._ratio = supermajority_ratio
        self._audit = audit
        self.last_ballots: list[dict] = []

    async def decide(self, content: str, meta: dict) -> Decision:
        import asyncio

        ballots = await asyncio.gather(*[v.vote(content, meta) for v in self._voters])
        self.last_ballots = []
        accepts = 0
        for voter, (accept, reason) in zip(self._voters, ballots):
            self.last_ballots.append({
                "agent_id": getattr(voter, "agent_id", ""), "accept": accept, "reason": reason})
            if accept:
                accepts += 1
            if self._audit is not None:
                await self._audit.record(
                    action="vote", level="auto", decision="accept" if accept else "reject",
                    agent_id=getattr(voter, "agent_id", ""), params={"content": content},
                    extra={"reason": reason})
        n = len(self._voters)
        if n == 0:
            # 空投票团不得放行:supermajority/weighted 下 0 >= 0*ratio 恒真,会
            # fail-open 成 promote(准入闸形同虚设)。无票可记一律保守 reject。
            passed = False
        elif self._rule == "supermajority":
            passed = accepts >= n * self._ratio
        elif self._rule == "weighted":
            passed = accepts >= n * self._ratio  # 权重表未配时等同 supermajority
        else:  # simple_majority
            passed = accepts * 2 > n
        return "promote" if passed else "reject"
