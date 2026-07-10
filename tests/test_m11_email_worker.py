"""PHASE3 M11 验收:收件驱动 worker(标签触发、仅草稿、全程审计、无 confirm 自动执行)。"""

from __future__ import annotations

from adapters.embedder import build_embedder
from adapters.memory import QdrantMemoryStore
from adapters.vectordb import QdrantAdapter
from core.approval import ApprovalQueue, Notifier
from core.audit import AuditLog
from core.config import ApprovalSettings, GmailPollSettings, PolicyRule
from core.email_ingest import EmailMemoryIngest
from core.email_worker import EmailWorker
from core.schemas import MultimodalInput
from tests.conftest import ScriptedLLM, make_fake_config


class FakeGmail:
    """伪 Gmail:list_labeled 返回预置邮件;draft_reply 记录草稿(绝不 send)。"""

    def __init__(self, messages):
        self._messages = messages
        self.drafts: list[dict] = []
        self.sent: list[dict] = []  # 必须始终为空

    async def list_labeled(self, label):
        return self._messages

    async def draft_reply(self, message_id, body):
        draft = {"message_id": message_id, "body": body}
        self.drafts.append(draft)
        return draft

    async def send(self, *a, **k):  # worker 绝不应调用
        self.sent.append((a, k))
        raise AssertionError("worker 不得发送邮件")


def build_store(cfg):
    embedder = build_embedder(cfg.embedder)
    db = QdrantAdapter(cfg.vectordb, dim=cfg.embedder.effective_dim)
    return QdrantMemoryStore(embedder, ScriptedLLM(), db, cfg)


def make_worker(tmp_path, store, gmail, extraction_reply):
    settings = ApprovalSettings(
        audit_path=str(tmp_path / "audit.jsonl"), default_level="confirm",
        policies=[PolicyRule(action="gmail_read", when={}, level="auto"),
                  PolicyRule(action="gmail_draft", when={}, level="auto"),
                  PolicyRule(action="gmail_send", when={}, level="confirm")])
    audit = AuditLog(settings.audit_path)
    queue = ApprovalQueue(settings, audit, Notifier(settings))
    ingest = EmailMemoryIngest(ScriptedLLM(replies=[extraction_reply]), store)
    poll = GmailPollSettings(enabled=True, label="agent-inbox")
    return EmailWorker(poll, gmail, ingest, queue, agent_id="agent-1"), audit


async def test_labeled_email_processed_into_draft_and_memory(tmp_path):
    cfg = make_fake_config(tmp_path)
    store = build_store(cfg)
    gmail = FakeGmail([{"id": "m1", "subject": "季度报告", "body": "张伟答应周五发报告"}])
    extraction = '{"promise":["张伟答应周五发送季度报告"],"preference":[],"relationship":[],"fact":[]}'
    worker, audit = make_worker(tmp_path, store, gmail, extraction)

    drafts = await worker.process_once()

    # 产出草稿,且从未发送
    assert len(drafts) == 1 and drafts[0]["message_id"] == "m1"
    assert gmail.drafts and gmail.sent == []
    # 邮件内容按需入记忆(source=gmail)
    hits = await store.search(MultimodalInput.text("张伟 季度报告"), k=5)
    assert any(h.meta.get("source") == "gmail" for h in hits)
    # 全程审计:read + draft 均在案,来源 email
    decisions = [(e["action"], e["decision"], e["source"]) for e in audit.read_all()]
    assert ("gmail_read", "executed", "email") in decisions
    assert ("gmail_draft", "executed", "email") in decisions
    # 无任何 confirm 级动作被自动执行
    assert all(not (e["level"] == "confirm" and e["decision"] in ("approved", "executed"))
               for e in audit.read_all())


async def test_worker_idempotent_on_seen_messages(tmp_path):
    cfg = make_fake_config(tmp_path)
    store = build_store(cfg)
    gmail = FakeGmail([{"id": "m1", "subject": "s", "body": "内容"}])
    worker, _ = make_worker(tmp_path, store, gmail,
                            '{"promise":[],"preference":[],"relationship":[],"fact":["事实"]}')
    await worker.process_once()
    await worker.process_once()  # 第二轮:m1 已见,不重复处理
    assert len(gmail.drafts) == 1


async def test_worker_disabled_by_default():
    poll = GmailPollSettings()  # enabled=False
    assert poll.enabled is False
    w = EmailWorker(poll, FakeGmail([]), None, None)
    w.start()  # enabled=False → 不启动后台任务
    assert w._task is None
