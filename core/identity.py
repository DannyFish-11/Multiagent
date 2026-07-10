"""Agent 身份层(PHASE2 M5.1)。

- agent_id:UUID v7(RFC 9562),实例创建时生成,终身不变
- keypair:Ed25519;私钥本地存储,文件权限 600;签名所有对外声明
- lineage:父代 agent_id 列表(M7 记忆遗传写入)
- profile:能力声明(模态/工具/记忆容量;payments 字段按附录 A 预留为空)

身份与记忆库绑定:memory_namespace() 给出以 agent_id 命名的记忆库目录/collection
命名;换脑(升级 LLM)不换身份;换身份必须新建记忆库。
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from core.errors import LayerError

IDENTITY_FILE = "identity.json"
PRIVATE_KEY_FILE = "agent_ed25519.key"

DEFAULT_PROFILE: dict[str, Any] = {
    "modalities": ["text", "image", "audio"],
    "tools": ["chat", "memory_store", "memory_search", "memory_consolidate", "multimodal_recall"],
    "memory_capacity": "unbounded",
    # 附录 A:支付能力预留字段,默认空;任何支付相关调用被策略层拒绝
    "payments": [],
}


def uuid7() -> str:
    """RFC 9562 UUID v7:48-bit 毫秒时间戳 + 74-bit 随机。"""
    ts_ms = time.time_ns() // 1_000_000
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = (ts_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76          # version 7
    value |= rand_a << 64
    value |= 0b10 << 62         # variant
    value |= rand_b
    return str(uuid.UUID(int=value))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def canonical_json(obj: Any) -> bytes:
    """确定性序列化 —— 签名与验签双方必须一致。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass
class AgentIdentity:
    agent_id: str
    private_key: Ed25519PrivateKey
    lineage: list[str] = field(default_factory=list)
    profile: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_PROFILE))
    created_at: float = field(default_factory=time.time)

    # ---- 持久化 ----

    @classmethod
    def load_or_create(cls, identity_dir: str | Path) -> "AgentIdentity":
        d = Path(identity_dir)
        d.mkdir(parents=True, exist_ok=True)
        meta_path = d / IDENTITY_FILE
        key_path = d / PRIVATE_KEY_FILE

        if meta_path.exists() and key_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            key_mode = key_path.stat().st_mode & 0o777
            if key_mode & 0o077:
                raise LayerError(
                    "L5", "identity",
                    f"私钥文件权限过宽 {oct(key_mode)}(要求 600):{key_path}",
                )
            private_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
            if not isinstance(private_key, Ed25519PrivateKey):
                raise LayerError("L5", "identity", f"{key_path} 不是 Ed25519 私钥")
            return cls(
                agent_id=meta["agent_id"],
                private_key=private_key,
                lineage=list(meta.get("lineage", [])),
                profile=dict(meta.get("profile", DEFAULT_PROFILE)),
                created_at=float(meta.get("created_at", time.time())),
            )

        ident = cls(agent_id=uuid7(), private_key=Ed25519PrivateKey.generate())
        ident.save(d)
        return ident

    def save(self, identity_dir: str | Path) -> None:
        d = Path(identity_dir)
        d.mkdir(parents=True, exist_ok=True)
        key_path = d / PRIVATE_KEY_FILE
        pem = self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        os.chmod(key_path, 0o600)
        (d / IDENTITY_FILE).write_text(
            json.dumps({
                "agent_id": self.agent_id,
                "lineage": self.lineage,
                "profile": self.profile,
                "created_at": self.created_at,
                "public_key": self.public_key_b64(),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ---- 密钥 / 签名 ----

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self.private_key.public_key()

    def public_key_b64(self) -> str:
        raw = self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        return _b64(raw)

    def sign(self, payload: Any) -> dict[str, str]:
        """对任意 JSON 负载做 JWS 风格(EdDSA)分离签名。

        返回 {protected, signature} —— 与 A2A v1.0 AgentCardSignature 字段对齐。
        """
        protected = _b64(canonical_json({"alg": "EdDSA", "kid": self.agent_id}))
        signing_input = protected.encode("ascii") + b"." + _b64(canonical_json(payload)).encode("ascii")
        sig = self.private_key.sign(signing_input)
        return {"protected": protected, "signature": _b64(sig)}

    def signed_envelope(self, payload: Any) -> dict[str, Any]:
        """MCP 工具响应 / A2A 消息使用的完整签名信封。"""
        sig = self.sign(payload)
        return {
            "payload": payload,
            "identity": {
                "agent_id": self.agent_id,
                "public_key": self.public_key_b64(),
            },
            **sig,
        }

    # ---- 记忆库绑定 ----

    def memory_namespace(self) -> str:
        """记忆库目录/collection 命名以 agent_id 为准(换脑不换身份)。"""
        return f"agent-{self.agent_id}"


def verify_signature(payload: Any, protected: str, signature: str, public_key_b64: str) -> bool:
    """验签(与 AgentIdentity.sign 对偶)。任何解析失败按验签失败处理。"""
    try:
        pub = Ed25519PublicKey.from_public_bytes(_unb64(public_key_b64))
        signing_input = protected.encode("ascii") + b"." + _b64(canonical_json(payload)).encode("ascii")
        pub.verify(_unb64(signature), signing_input)
        return True
    except (InvalidSignature, ValueError, KeyError):
        return False


def verify_envelope(envelope: dict[str, Any]) -> bool:
    try:
        return verify_signature(
            envelope["payload"], envelope["protected"], envelope["signature"],
            envelope["identity"]["public_key"],
        )
    except (KeyError, TypeError):
        return False
