"""PHASE2.5 M-A 验收:LLM API 化(全部 mock,不花真钱)。"""

from __future__ import annotations

import json

import httpx
import pytest

from adapters.cost_ledger import CostLedger
from adapters.llm import OpenAICompatAdapter, VLLMOpenAIAdapter, build_llm_client
from core.config import BudgetSettings, LLMEndpoint, LLMRoleSettings, LLMSettings
from core.errors import LayerError
from core.schemas import Message
from tests.conftest import make_fake_config


def role(base="http://primary/v1", model="model-a", fallbacks=(), threshold=3, retries=0):
    return LLMRoleSettings(
        base_url=base, api_key="k", model=model,
        fallbacks=[LLMEndpoint(base_url=u, api_key="k2", model=m) for u, m in fallbacks],
        failover_threshold=threshold, max_retries=retries, retry_backoff_s=0.0,
    )


def ok_response(content="OK", model="model-a", pt=10, ct=5):
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct},
        "model": model,
    })


# ---------------------------------------------------------------- 重试

async def test_retry_recovers_from_transient_errors():
    """瞬时 5xx 在 max_retries 内自动重试成功。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, text="transient")
        return ok_response("恢复成功")

    llm = OpenAICompatAdapter(role(retries=3), transport=httpx.MockTransport(handler))
    reply = await llm.chat([Message(role="user", content="hi")])
    assert reply == "恢复成功"
    assert calls["n"] == 3


# ---------------------------------------------------------------- 备用端点

async def test_failover_after_consecutive_failures():
    """主端点连续失败 N 次后自动切到 fallback;切换在 last_meta 标注。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "primary":
            return httpx.Response(503, text="primary down")
        return ok_response("来自备胎", model="model-b")

    llm = OpenAICompatAdapter(
        role(fallbacks=[("http://backup/v1", "model-b")], threshold=2, retries=0),
        transport=httpx.MockTransport(handler),
    )
    # 第 1 次:主失败 1 次(< 阈值 2),不切换 → 整体报错
    with pytest.raises(LayerError):
        await llm.chat([Message(role="user", content="hi")])
    # 第 2 次:主失败达到阈值 → 自动落到 fallback 成功
    reply = await llm.chat([Message(role="user", content="hi")])
    assert reply == "来自备胎"
    assert llm.last_meta["failover"] is True
    assert llm.last_meta["endpoint"] == "http://backup/v1"
    # 此后粘住可用端点
    reply = await llm.chat([Message(role="user", content="hi")])
    assert llm.last_meta["endpoint"] == "http://backup/v1"
    assert llm.last_meta["failover"] is False
    del reply


async def test_all_endpoints_exhausted_raises_l0():
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    llm = OpenAICompatAdapter(
        role(fallbacks=[("http://backup/v1", "model-b")], threshold=1, retries=0),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LayerError) as exc:
        await llm.chat([Message(role="user", content="hi")])
    assert exc.value.layer == "L0" and "耗尽" in str(exc.value)


# ---------------------------------------------------------------- 用量与预算

async def test_usage_recorded_to_ledger(tmp_path):
    ledger = CostLedger({"model-a": {"input": 1.0, "output": 2.0}}, 100.0,
                        tmp_path / "ledger.json")
    llm = OpenAICompatAdapter(role(), ledger=ledger,
                              transport=httpx.MockTransport(lambda r: ok_response(pt=1000, ct=500)))
    await llm.chat([Message(role="user", content="hi")])
    snap = ledger.snapshot()
    entry = snap["entries"]["http://primary/v1::model-a"]
    assert entry["prompt_tokens"] == 1000 and entry["completion_tokens"] == 500
    # 1000/1e6*1.0 + 500/1e6*2.0 = 0.002
    assert abs(snap["total_usd"] - 0.002) < 1e-9
    assert llm.last_meta["usage"]["prompt_tokens"] == 1000


async def test_budget_exceeded_rejects_with_usage(tmp_path):
    """超预算后新请求被拒,报错含当日用量(验收⑤的单测版)。"""
    ledger = CostLedger({"model-a": {"input": 1.0, "output": 2.0}}, 0.01,
                        tmp_path / "ledger.json")
    llm = OpenAICompatAdapter(role(), ledger=ledger,
                              transport=httpx.MockTransport(lambda r: ok_response(pt=9_000_000, ct=1_000_000)))
    await llm.chat([Message(role="user", content="hi")])  # 记入 $11
    with pytest.raises(LayerError) as exc:
        await llm.chat([Message(role="user", content="again")])
    msg = str(exc.value)
    assert "日预算" in msg and "11.0000" in msg and "0.0100" in msg


def test_ledger_persistence_and_rollover(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = CostLedger({"m": {"input": 1.0, "output": 1.0}}, 10.0, path)
    ledger.record("ep", "m", 1_000_000)
    # 重建实例:同日用量保留(volume 场景)
    again = CostLedger({"m": {"input": 1.0, "output": 1.0}}, 10.0, path)
    assert again.today_usd() == 1.0
    # 换日:清零
    state = json.loads(path.read_text())
    state["date"] = "2000-01-01"
    path.write_text(json.dumps(state))
    fresh = CostLedger({"m": {"input": 1.0, "output": 1.0}}, 10.0, path)
    assert fresh.today_usd() == 0.0


# ---------------------------------------------------------------- 多模态与装配

async def test_multimodal_parts_passthrough():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return ok_response()

    llm = OpenAICompatAdapter(role(), transport=httpx.MockTransport(handler))
    await llm.chat([Message(role="user", content=[
        {"type": "text", "text": "这是什么"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ])])
    parts = captured["json"]["messages"][0]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png")


def test_build_llm_client_mode_switch(tmp_path):
    """mode=local 一键切回 vLLM 路径;mode=api 双角色可分开配置。"""
    cfg = make_fake_config(tmp_path)
    assert isinstance(build_llm_client(cfg), VLLMOpenAIAdapter)

    cfg.llm = LLMSettings(
        mode="api",
        chat=LLMRoleSettings(base_url="http://chat/v1", api_key="k", model="chat-m"),
        memory=LLMRoleSettings(base_url="http://mem/v1", api_key="k", model="mem-m"),
    )
    chat = build_llm_client(cfg, role="chat")
    mem = build_llm_client(cfg, role="memory")
    assert isinstance(chat, OpenAICompatAdapter) and chat.model == "chat-m"
    assert mem.model == "mem-m"
    # memory 角色未配置时回落 chat 端点
    cfg.llm.memory = LLMRoleSettings()
    assert build_llm_client(cfg, role="memory").model == "chat-m"


def test_api_mode_without_base_url_is_stop_point(tmp_path):
    cfg = make_fake_config(tmp_path)
    cfg.llm = LLMSettings(mode="api")
    cfg.budget = BudgetSettings(ledger_path=str(tmp_path / "l.json"))
    with pytest.raises(LayerError) as exc:
        build_llm_client(cfg)
    assert "停点" in str(exc.value)
