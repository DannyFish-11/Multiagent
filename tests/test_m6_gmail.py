"""Milestone 6 验收:Gmail 治理分级 + 邮件记忆管道 + 行动记忆闭环。"""

from __future__ import annotations

import sys

from adapters.embedder import build_embedder
from adapters.memory import QdrantMemoryStore
from adapters.vectordb import QdrantAdapter
from core.action_memory import ActionRecorder
from core.email_ingest import CATEGORIES, EmailMemoryIngest
from core.schemas import MultimodalInput
from tests.conftest import PROJECT_ROOT, ScriptedLLM, make_fake_config

sys.path.insert(0, str(PROJECT_ROOT / "omnigent"))

from omnigent_policies.gmail_governance import classify, enforce  # noqa: E402
from omnigent_policies.payments_guard import enforce as payments_enforce  # noqa: E402


def build_store(cfg):
    embedder = build_embedder(cfg.embedder)
    db = QdrantAdapter(cfg.vectordb, dim=cfg.embedder.effective_dim)
    return QdrantMemoryStore(embedder, ScriptedLLM(), db, cfg)


# ---------------------------------------------------------------- 6.1 治理分级

def test_gmail_permission_tiers():
    # read/search/summarize → 自动放行
    for tool in ("gmail_read_email", "gmail_search_emails", "gmail_list_labels", "gmail_summarize"):
        assert classify(tool) == "allow", tool
    # draft → 放行(草稿不发出)
    assert classify("gmail_create_draft") == "allow"
    # send/delete/archive → 每次 ask,无例外;send_draft 亦须 ask
    for tool in ("gmail_send_email", "gmail_send_draft", "gmail_delete_email",
                 "gmail_archive_email", "gmail_batch_modify"):
        assert classify(tool) == "ask", tool
    # 未识别的 Gmail 工具 → 保守 ask
    assert classify("gmail_mystery_operation") == "ask"


def test_gmail_policy_enforce_shape():
    verdict = enforce({"tool_name": "gmail_send_email"})
    assert verdict["action"] == "ask" and "人工确认" in verdict["reason"]
    assert enforce({"tool_name": "gmail_read_email"})["action"] == "allow"
    # 非 Gmail 工具不受本策略约束
    assert enforce({"tool_name": "memory_search"})["action"] == "allow"


def test_payments_blanket_deny():
    """附录 A:任何资金/钱包/支付关键词一律拒绝并告警(默认拒付)。"""
    for tool, args in (("stripe_create_payment", {}), ("wallet_transfer", {}),
                       ("some_tool", {"note": "帮我付款 100 元"}),
                       ("agent_task", {"skill": "x402_settle"})):
        verdict = payments_enforce({"tool_name": tool, "arguments": args})
        assert verdict["action"] == "deny", (tool, args)
        assert "拒付" in verdict["reason"]
    assert payments_enforce({"tool_name": "memory_search", "arguments": {"query": "天气"}})["action"] == "allow"
    # 误杀回归:payload/repayment 等含 pay 子串的普通词不得触发拒付
    for tool, args in (("gmail_read_payload", {}), ("http_parse", {"field": "payload"}),
                       ("loan_notes", {"text": "repayment schedule discussion"})):
        assert payments_enforce({"tool_name": tool, "arguments": args})["action"] == "allow", (tool, args)


def test_gmail_mcp_mount_config():
    import yaml

    mount = yaml.safe_load((PROJECT_ROOT / "omnigent" / "memory-agent" / "tools" / "mcp" / "gmail.yaml")
                           .read_text(encoding="utf-8"))
    assert mount["transport"] == "stdio"
    assert mount["command"] and mount["args"]


# ---------------------------------------------------------------- 6.1 邮件→记忆

async def test_email_ingest_four_categories(tmp_path):
    cfg = make_fake_config(tmp_path)
    store = build_store(cfg)
    llm = ScriptedLLM(replies=['''{
        "promise": ["答应周五前把季度报告发给张伟"],
        "preference": ["张伟偏好 PDF 格式的附件"],
        "relationship": ["张伟是市场部负责人"],
        "fact": ["季度评审会定在下周三上午十点"]
    }'''])
    ingest = EmailMemoryIngest(llm, store)
    stored = await ingest.ingest("(某封邮件正文)", message_id="msg-123")

    assert set(stored.keys()) == set(CATEGORIES)
    assert all(len(v) == 1 for v in stored.values())
    # 来源标注:source=gmail + message_id;且按需吸取(只有这 4 条)
    hits = await store.search(MultimodalInput.text("张伟 报告"), k=10)
    gmail_hits = [h for h in hits if h.meta.get("source") == "gmail"]
    assert gmail_hits and all(h.meta["message_id"] == "msg-123" for h in gmail_hits)
    categories = {h.meta["category"] for h in gmail_hits}
    assert categories <= set(CATEGORIES)


# ---------------------------------------------------------------- 6.2 行动记忆闭环

async def test_action_memory_loop(tmp_path):
    cfg = make_fake_config(tmp_path)
    store = build_store(cfg)
    recorder = ActionRecorder(store)

    await recorder.record(action="预订餐厅「老王川菜」", result="成功订到周六晚 7 点",
                          feedback="用户嫌这家太吵", tool="restaurant_booking")
    await store.add(MultimodalInput.text("无关记忆:今天天气晴"), {})

    # 行动前检索:相关 ActionMemory 注入决策上下文
    relevant = await recorder.recall_relevant("帮我订个餐厅")
    assert relevant, "行动前未召回任何行动经验"
    assert any("嫌这家太吵" in h.content for h in relevant)
    assert all(h.meta.get("kind") == "action" for h in relevant)

    block = ActionRecorder.context_block(relevant)
    assert "相关行动经验" in block and "太吵" in block


async def test_action_memory_is_private(tmp_path):
    cfg = make_fake_config(tmp_path)
    store = build_store(cfg)
    recorder = ActionRecorder(store)
    mem_id = await recorder.record(action="发送了一封草稿", result="ok")
    assert mem_id
    hits = await store.search(MultimodalInput.text("发送了一封草稿"), k=3)
    assert hits[0].meta.get("visibility") == "private"
