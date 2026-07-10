"""M20 A1 无 key demo:零密钥 / 零 GPU / 零 docker 演示记忆存取闭环。

自设 demo 档配置(仅当未被外部覆盖时):
  llm.mode=echo             —— 回显"检索到的记忆 + 问题",不做真实推理
  embedder.backend=fake     —— 确定性哈希嵌入(词面重叠可检索,无语义)
  vectordb.mode=memory      —— 进程内向量库,免 Qdrant/docker
  memory.extraction=verbatim—— 原文入库(echo LLM 不具抽取能力)

流程:存入"我的猫叫 Benjamin" → 新一轮问"我的猫叫什么" → 打印检索命中。
证明:存入→检索→注入 prompt→复述 这条链路打通(**不代表真实效果**)。

用法:`make demo` 或 `uv run python scripts/demo.py`
"""

from __future__ import annotations

import os


def _apply_demo_profile() -> None:
    """仅设默认值(setdefault),外部已显式配置的键一律不覆盖。"""
    os.environ.setdefault("MEMORY_AGENT_LLM__MODE", "echo")
    os.environ.setdefault("MEMORY_AGENT_EMBEDDER__BACKEND", "fake")
    os.environ.setdefault("MEMORY_AGENT_VECTORDB__MODE", "memory")
    os.environ.setdefault("MEMORY_AGENT_MEMORY__EXTRACTION", "verbatim")


def main() -> int:
    _apply_demo_profile()

    # 延迟导入:确保 demo 档环境变量在 config 首次加载前生效
    from fastapi.testclient import TestClient

    from core.config import load_config
    from services.api import create_app

    cfg = load_config()
    print("=" * 64)
    print("memory-agent · 无 key demo(M20 A1)")
    print(f"  llm.mode           = {cfg.llm.mode}")
    print(f"  embedder.backend   = {cfg.embedder.backend}")
    print(f"  vectordb.mode      = {cfg.vectordb.mode}")
    print(f"  memory.extraction  = {cfg.memory.extraction}")
    print("  ⚠️  demo 档:哈希嵌入 + echo 回显,仅验证存取链路,不代表真实检索质量。")
    print("=" * 64)

    app = create_app(cfg)
    fact = "我的猫叫 Benjamin"
    question = "我的猫叫什么?"

    with TestClient(app) as client:  # with 触发 lifespan 装配(fake 后端无外呼)
        h = client.get("/healthz")
        print(f"[healthz] HTTP {h.status_code} status={h.json().get('status')} "
              f"layers={h.json().get('layers')}")

        add = client.post("/memory/add", json={
            "input": {"type": "text", "content": fact}, "meta": {"session_id": "demo"}})
        print(f"[存入记忆] HTTP {add.status_code} → {fact!r} ids={add.json().get('ids')}")

        chat = client.post("/chat", json={"message": question, "session_id": "demo"})
        body = chat.json()
        reply = body.get("reply", "")
        used = body.get("memories_used", [])
        print(f"[提问] {question!r}")
        print(f"[回复] {reply}")
        print(f"[检索命中] {[m.get('content') for m in used]}")

    hit = "Benjamin" in reply and any("Benjamin" in (m.get("content") or "") for m in used)
    print("=" * 64)
    if hit:
        print("✅ 记忆闭环跑通:存入的事实被检索命中并复述(零 key / 零 docker)。")
        return 0
    print("❌ 记忆闭环未命中——demo 档链路异常,请检查。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
