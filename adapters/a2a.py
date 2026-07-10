"""A2A 协议适配层(M5.2,a2a-sdk ~=1.0.3,遵守 PHASE 1 adapter 红线)。

对 a2a-sdk 的全部知识收敛在本文件。核心逻辑(core/)只接触本文件导出的
纯数据结构与两个 Adapter 类。

签名方案(与 a2a-sdk 1.0.3 实际类型对齐,见其 a2a/types/a2a_pb2.pyi):
AgentCardSignature{protected, signature, header} 为 JWS 分离签名 —— 直接复用
core.identity.AgentIdentity.sign() 产出的 {protected, signature},header 携带
agent_id 与公钥(自包含验签;生产环境应换成 DID/证书链,本阶段自签)。

服务端能力(委托任务):chat / memory_search / multimodal_recall。
未知 agent_id 的委托默认拒绝执行并要求人工批准(TrustStore 白名单放行)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.errors import LayerError
from core.identity import AgentIdentity, verify_signature
from core.schemas import MultimodalInput

if TYPE_CHECKING:
    from adapters.memory import MemoryStore
    from core.trust import TrustStore

SKILLS = ("chat", "memory_search", "multimodal_recall")


def _require_sdk():
    try:
        import a2a  # noqa: F401
        from a2a import types as a2a_types

        return a2a_types
    except ImportError as exc:
        raise LayerError(
            "L5", "a2a",
            "a2a-sdk 未安装:uv sync --extra a2a(锁定 ~=1.0.3)",
        ) from exc


# ---------------------------------------------------------------- Agent Card

@dataclass
class CardData:
    """SDK 无关的 Agent Card 数据(签名作用于此规范化负载)。"""

    agent_id: str
    name: str
    description: str
    url: str
    skills: list[str] = field(default_factory=lambda: list(SKILLS))
    payments: list = field(default_factory=list)  # 附录 A 预留,恒为空
    public_key: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "skills": self.skills,
            "payments": self.payments,
            "public_key": self.public_key,
        }


def build_signed_card(identity: AgentIdentity, base_url: str,
                      name: str = "memory-agent") -> dict[str, Any]:
    """构造签名 Agent Card(dict 形式;to_proto 见 A2AServerAdapter)。"""
    card = CardData(
        agent_id=identity.agent_id,
        name=name,
        description="拥有终身多模态记忆的 agent:接受回忆/检索类任务委托",
        url=base_url,
        public_key=identity.public_key_b64(),
    )
    sig = identity.sign(card.payload())
    return {
        "card": card.payload(),
        "signatures": [{
            "protected": sig["protected"],
            "signature": sig["signature"],
            "header": {"agent_id": identity.agent_id, "public_key": identity.public_key_b64()},
        }],
    }


def verify_signed_card(signed_card: dict[str, Any]) -> bool:
    """验签他人 Agent Card(公钥取自 card.public_key,须与签名头一致)。"""
    try:
        card = signed_card["card"]
        sig = signed_card["signatures"][0]
        if sig["header"]["public_key"] != card["public_key"]:
            return False
        return verify_signature(card, sig["protected"], sig["signature"], card["public_key"])
    except (KeyError, IndexError, TypeError):
        return False


# ---------------------------------------------------------------- 服务端

class A2AServerAdapter:
    """A2A Server:发布签名 Agent Card,受理回忆/检索类委托任务。

    handle_task 为传输无关的任务处理核心(便于测试);serve() 把它接到
    a2a-sdk 的 AgentExecutor/HTTP 栈上(需 --extra a2a,目标机器验证)。
    """

    def __init__(self, identity: AgentIdentity, memory: "MemoryStore",
                 trust: "TrustStore", base_url: str) -> None:
        self._identity = identity
        self._memory = memory
        self._trust = trust
        self._base_url = base_url
        self.signed_card = build_signed_card(identity, base_url)

    async def handle_task(self, skill: str, params: dict[str, Any],
                          from_agent_id: str | None, envelope: dict[str, Any] | None = None,
                          ) -> dict[str, Any]:
        """处理一次委托。返回带本方身份签名的响应信封。

        鉴权:未知 agent_id 默认拒绝(需人工批准/加白);白名单放行。
        """
        if not from_agent_id or not await self._trust.is_trusted(from_agent_id):
            return self._identity.signed_envelope({
                "status": "approval_required",
                "reason": f"未知 agent_id={from_agent_id!r} 的委托默认需人工批准"
                          "(TrustStore.trust() 加白后放行)",
            })
        if skill not in SKILLS:
            return self._identity.signed_envelope(
                {"status": "rejected", "reason": f"不支持的 skill: {skill}"})

        if skill in ("memory_search", "multimodal_recall"):
            query = params.get("query", "")
            k = int(params.get("k", 5))
            hits = await self._memory.search(MultimodalInput.text(query), k=k)
            # 委托方只能看到共享池与显式 shared 的结果之外?——本阶段策略:
            # 仅返回 visibility=shared 的记忆,私有记忆不外泄
            shared_hits = [h.model_dump() for h in hits if h.meta.get("visibility") == "shared"]
            return self._identity.signed_envelope(
                {"status": "ok", "skill": skill, "hits": shared_hits})

        return self._identity.signed_envelope(
            {"status": "rejected", "reason": "chat 委托须经宿主(Omnigent)会话通道"})

    def serve(self, host: str, port: int) -> None:  # pragma: no cover - 需 a2a extra + 目标机器
        a2a_types = _require_sdk()
        import uvicorn
        from a2a.server.agent_execution import AgentExecutor
        from a2a.server.apps import A2AStarletteApplication  # type: ignore[attr-defined]

        card_dict = self.signed_card["card"]
        proto_card = a2a_types.AgentCard(
            protocol_version="1.0",
            name=card_dict["name"],
            description=card_dict["description"],
            url=card_dict["url"],
            version="1.0.0",
            skills=[a2a_types.AgentSkill(id=s, name=s, description=s, tags=[s])
                    for s in card_dict["skills"]],
            signatures=[a2a_types.AgentCardSignature(
                protected=self.signed_card["signatures"][0]["protected"],
                signature=self.signed_card["signatures"][0]["signature"],
            )],
        )
        adapter = self

        class _Executor(AgentExecutor):  # type: ignore[misc]
            async def execute(self, context, event_queue):  # noqa: ANN001
                text = context.get_user_input()
                try:
                    req = json.loads(text)
                except json.JSONDecodeError:
                    req = {"skill": "memory_search", "params": {"query": text}}
                result = await adapter.handle_task(
                    req.get("skill", "memory_search"), req.get("params", {}),
                    req.get("from_agent_id"))
                from a2a.utils import new_agent_text_message  # type: ignore[attr-defined]

                await event_queue.enqueue_event(
                    new_agent_text_message(json.dumps(result, ensure_ascii=False)))

            async def cancel(self, context, event_queue):  # noqa: ANN001
                raise NotImplementedError

        app = A2AStarletteApplication(agent_card=proto_card, executor=_Executor())
        uvicorn.run(app.build(), host=host, port=port)


# ---------------------------------------------------------------- 客户端

class A2AClientAdapter:
    """A2A Client:读取并验签他人 Agent Card,发起任务委托。"""

    def __init__(self, identity: AgentIdentity) -> None:
        self._identity = identity

    async def fetch_and_verify_card(self, base_url: str) -> dict[str, Any]:
        """经 HTTP 获取对方 signed card(自有 JSON 端点或 A2A well-known)并验签。"""
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/identity/card")
            if resp.status_code != 200:
                raise LayerError("L5", "a2a-client", f"获取 Agent Card 失败 HTTP {resp.status_code}")
            signed_card = resp.json()
        if not verify_signed_card(signed_card):
            raise LayerError("L5", "a2a-client", f"Agent Card 验签失败: {base_url}")
        return signed_card

    def build_delegation(self, skill: str, params: dict[str, Any]) -> dict[str, Any]:
        """构造带本方签名的委托消息体。"""
        return self._identity.signed_envelope({
            "skill": skill, "params": params, "from_agent_id": self._identity.agent_id,
        })

    async def delegate_via_sdk(self, card_url: str, skill: str,
                               params: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        """经 a2a-sdk 正式通道委托(需 --extra a2a,目标机器验证)。"""
        _require_sdk()
        from a2a.client import ClientFactory  # noqa: F401  # 实际组装在目标机器冒烟时完成

        raise LayerError("L5", "a2a-client", "SDK 通道的端到端联调属目标机器冒烟项(M5 验收③)")
