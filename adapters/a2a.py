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
            query = str(params.get("query", ""))
            try:
                k = max(1, min(50, int(params.get("k", 5))))
            except (TypeError, ValueError):
                k = 5
            hits = await self._memory.search(MultimodalInput.text(query), k=k)
            # 本阶段外泄策略:仅返回 visibility=shared 的记忆,私有记忆绝不外泄
            shared_hits = [h.model_dump() for h in hits if h.meta.get("visibility") == "shared"]
            return self._identity.signed_envelope(
                {"status": "ok", "skill": skill, "hits": shared_hits})

        return self._identity.signed_envelope(
            {"status": "rejected", "reason": "chat 委托须经宿主(Omnigent)会话通道"})

    def build_proto_card(self):
        """dict card → a2a-sdk 1.0.3 proto AgentCard(字段以其 a2a_pb2 实际定义为准:
        无 url 顶层字段,端点挂在 supported_interfaces;签名为 AgentCardSignature)。"""
        a2a_types = _require_sdk()
        card_dict = self.signed_card["card"]
        return a2a_types.AgentCard(
            name=card_dict["name"],
            description=card_dict["description"],
            version="1.0.0",
            supported_interfaces=[a2a_types.AgentInterface(
                url=card_dict["url"], protocol_binding="JSONRPC", protocol_version="1.0",
            )],
            skills=[a2a_types.AgentSkill(id=s, name=s, description=s, tags=[s])
                    for s in card_dict["skills"]],
            signatures=[a2a_types.AgentCardSignature(
                protected=self.signed_card["signatures"][0]["protected"],
                signature=self.signed_card["signatures"][0]["signature"],
            )],
        )

    def build_app(self):
        """组装 a2a-sdk 1.0.3 的 Starlette 应用(JSONRPC 方法 SendMessage +
        /.well-known/agent-card.json)。可直接被测试客户端驱动,serve() 仅负责起端口。"""
        a2a_types = _require_sdk()
        import uuid as _uuid

        from a2a.server.agent_execution import AgentExecutor
        from a2a.server.request_handlers import DefaultRequestHandler
        from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
        from a2a.server.tasks import InMemoryTaskStore
        from starlette.applications import Starlette

        proto_card = self.build_proto_card()
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
                await event_queue.enqueue_event(a2a_types.Message(
                    message_id=_uuid.uuid4().hex,
                    role=a2a_types.Role.ROLE_AGENT,
                    parts=[a2a_types.Part(text=json.dumps(result, ensure_ascii=False))],
                ))

            async def cancel(self, context, event_queue):  # noqa: ANN001
                raise NotImplementedError

        handler = DefaultRequestHandler(
            agent_executor=_Executor(), task_store=InMemoryTaskStore(), agent_card=proto_card,
        )
        routes = create_agent_card_routes(proto_card) + create_jsonrpc_routes(handler, rpc_url="/")
        return Starlette(routes=routes)

    def serve(self, host: str, port: int) -> None:  # pragma: no cover - 起端口属部署动作
        import uvicorn

        uvicorn.run(self.build_app(), host=host, port=port)


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

    async def delegate(self, rpc_url: str, skill: str, params: dict[str, Any],
                       transport=None) -> dict[str, Any]:
        """经 A2A v1.0 JSONRPC 通道(SendMessage)发起带签名的任务委托。

        返回对方的签名信封(调用方应以 verify_envelope 验签)。
        transport 参数供测试注入 ASGITransport。
        """
        import uuid as _uuid

        import httpx

        delegation = self.build_delegation(skill, params)["payload"]
        body = {
            "jsonrpc": "2.0", "id": _uuid.uuid4().hex, "method": "SendMessage",
            "params": {"message": {
                "messageId": _uuid.uuid4().hex, "role": "ROLE_USER",
                "parts": [{"text": json.dumps(delegation, ensure_ascii=False)}],
            }},
        }
        async with httpx.AsyncClient(transport=transport, timeout=60) as client:
            resp = await client.post(rpc_url, json=body, headers={"A2A-Version": "1.0"})
        if resp.status_code != 200:
            raise LayerError("L5", "a2a-client", f"委托失败 HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if "error" in data:
            raise LayerError("L5", "a2a-client", f"JSONRPC 错误: {data['error']}")
        try:
            parts = data["result"]["message"]["parts"]
            return json.loads(parts[0]["text"])
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise LayerError("L5", "a2a-client", f"响应结构异常: {data}") from exc
