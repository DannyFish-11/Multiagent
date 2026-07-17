"""M11 验收:收件驱动 worker。

流程:打标签新邮件 → 阅读(auto) → 按需入记忆 → 草拟回复(绝不发送)。
这里用假 Gmail 适配器 + 内存 Qdrant,验证整条流程与审批级别。
"""

from __future__ import annotations

from adapters.llm import build_llm_client
from adapters.memory import QdrantMemoryStore, build_memory_store
from core.approval import ApprovalQueue
from core.config import load_config
from core.email_ingest import EmailMemoryIngest
from core.email_worker import EmailWorker
from tests.conftest import ScriptedLLM


def make_fake_config(tmp_path, **overrides):
    base = dict(
        vectordb={"mode": "memory", "collection": "test", "path": str(tmp_path / "vdb")},
        embedder={"backend": "fake"},
        llm={"mode": "api", "chat": {"base_url": "http://x", "model": "m"}},
        memory={"extraction": "llm"},
    )
    base.update(overrides)
    return load_config(**base)


def build_store(cfg):
    return QdrantMemoryStore(
        cfg.vectordb, cfg.embedder,
        ScriptedLLM(),           # fact extraction(每文本记忆一次)
        build_llm_client(cfg, "chat"),
        cfg,
    )


class FakeGmail:
    def __init__(self, messages):
        self._messages = messages  # [{"id","subject","body"}]
        self.drafts: list[tuple[str, str]] = []

    async def list_labeled(self, label):
        return self._messages

    async def draft_reply(self, message_id, body):
        self.drafts.append((message_id, body))
        return {"draft_id": f"d-{message_id}", "body": body}


def make_worker(tmp_path, store, gmail, extraction_reply='{"promise":[],"preference":[],"relationship":[],"fact":["x"]}'):
    cfg = load_config(vectordb={"mode": "memory", "collection": "test", "path": str(tmp_path / "vdb")},
                      embedder={"backend": "fake"},
                      llm={"mode": "api", "chat": {"base_url": "http://x", "model": "m"}},
                      memory={"extraction": "llm"},
                      gmail_poll={"enabled": True, "label": "agent", "interval_s": 60})
    queue = ApprovalQueue(cfg.approval, cfg.rules, cfg.trust, audit=None)
    ingest = EmailMemoryIngest(ScriptedLLM(replies=[extraction_reply]), store)
    worker = EmailWorker(cfg.gmail_poll, gmail, ingest, queue, agent_id="a1")
    return worker, queue


async def test_labeled_email_processed_into_draft_and_memory(tmp_path):
    cfg = make_fake_config(tmp_path)
    store = build_store(cfg)
    gmail = FakeGmail([
        {"id": "m1", "subject": "周报", "body": "张伟答应周五发报告"},
    ])
    extraction = '{"promise":["张伟答应周五发送季度报告"],"preference":[],"relationship":[],"fact":[]}'
    worker, queue = make_worker(tmp_path, store, gmail, extraction)

    drafts = await worker.process_once()
    assert len(drafts) == 1
    assert gmail.drafts and gmail.drafts[0][0] == "m1"  # 起草了回复
    # 记忆已写入(承诺)
    hits = await store.search("周五 报告", k=5)
    assert any("周五" in h.content for h in hits)
    # 审批级别:gmail_read / gmail_draft 均为 auto(零打扰)
    for action in ("gmail_read", "gmail_draft"):
        assert queue.engine.level_of(action, "email") == "auto"


async def test_worker_idempotent_on_seen_messages(tmp_path):
    cfg = make_fake_config(tmp_path)
    store = build_store(cfg)
    gmail = FakeGmail([{"id": "m1", "subject": "s", "body": "内容"}])
    worker, _ = make_worker(tmp_path, store, gmail)
    await worker.process_once()
    drafts = await worker.process_once()   # 第二轮:同一邮件不再处理
    assert drafts == [] and len(gmail.drafts) == 1


async def test_worker_never_sends(tmp_path):
    """红线:worker 只起草,不发送。FakeGmail 甚至没有 send 方法;
    若未来有人误调,本测试以属性缺失天然失败。"""
    cfg = make_fake_config(tmp_path)
    store = build_store(cfg)
    gmail = FakeGmail([{"id": "m1", "subject": "s", "body": "内容"}])
    worker, _ = make_worker(tmp_path, store, gmail)
    await worker.process_once()
    assert not hasattr(gmail, "send")


async def test_failed_message_retried_next_round(tmp_path):
    """回归:处理中途失败的邮件不得被静默永久跳过。

    修复前:_seen.add(mid) 在处理**之前**——入记忆/草拟抛错后该邮件已标记,
    下一轮直接跳过(草稿与记忆双双丢失)。修复后:全部成功才标记,失败下轮重试。
    """
    import pytest

    from core.errors import LayerError

    cfg = make_fake_config(tmp_path)
    store = build_store(cfg)
    gmail = FakeGmail([{"id": "m1", "subject": "s", "body": "张伟答应周五发报告"}])
    ok = '{"promise":["张伟答应周五发送季度报告"],"preference":[],"relationship":[],"fact":[]}'
    worker, _ = make_worker(tmp_path, store, gmail, "not-json")   # 首次抽取输出非法
    # 补上第二轮的成功回复(ScriptedLLM 按序弹出)
    worker._ingest._llm.replies.append(ok)  # noqa: SLF001 - 测试替身按序供稿

    with pytest.raises(LayerError):
        await worker.process_once()          # 首轮:抽取失败
    assert gmail.drafts == []                # 无草稿
    drafts = await worker.process_once()     # 次轮:同一邮件被重试并成功
    assert len(drafts) == 1 and len(gmail.drafts) == 1


async def test_seen_cache_bounded_fifo(tmp_path):
    """回归:长跑 worker 的去重缓存有界(先进先出逐出),不无界增长。"""
    cfg = make_fake_config(tmp_path)
    store = build_store(cfg)
    msgs = [{"id": f"m{i}", "subject": "s", "body": "内容"} for i in range(3)]
    gmail = FakeGmail(msgs)
    extraction = '{"promise":[],"preference":[],"relationship":[],"fact":["事实"]}'
    worker, _ = make_worker(tmp_path, store, gmail, extraction)
    worker._ingest._llm.replies.extend([extraction, extraction])  # noqa: SLF001
    worker._seen_capacity = 2                # 小窗口验证逐出语义

    drafts = await worker.process_once()
    assert len(drafts) == 3                  # 三封全部处理
    assert len(worker._seen) == 2            # 缓存封顶
    assert "m0" not in worker._seen and "m2" in worker._seen  # 最老被逐出
