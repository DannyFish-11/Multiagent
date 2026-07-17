"""第三轮排查:并发与生命周期。

覆盖:并发对话下的后台记忆写入与检索竞争、后台写失败的隔离性、
多实例挂同一共享池的读写一致性、并发上交、检索事件日志的并发追加、
uuid7 突发唯一性、整合与写入并发时的存活性。
"""

from __future__ import annotations

import asyncio

from adapters.embedder import build_embedder
from adapters.memory import QdrantMemoryStore
from adapters.vectordb import QdrantAdapter
from core.agent import MemoryAgent
from core.identity import uuid7
from core.metabolism import RetrievalLogger
from core.schemas import MultimodalInput
from tests.conftest import EchoMemoryLLM, ScriptedLLM, make_fake_config


def build_store(cfg, with_shared: bool = False):
    embedder = build_embedder(cfg.embedder)
    db = QdrantAdapter(cfg.vectordb, dim=cfg.embedder.effective_dim)
    shared = None
    if with_shared:
        shared = QdrantAdapter(cfg.vectordb, dim=cfg.embedder.effective_dim,
                               collection=cfg.memory.shared_collection, share_client_from=db)
    return QdrantMemoryStore(embedder, ScriptedLLM(), db, cfg, shared_db=shared), db, shared


class FlakyStore:
    """包装 MemoryStore:第 fail_on 次 add 抛异常,其余透传。"""

    def __init__(self, inner, fail_on: int) -> None:
        self._inner = inner
        self._count = 0
        self._fail_on = fail_on

    async def add(self, content, meta):
        self._count += 1
        if self._count == self._fail_on:
            raise RuntimeError("模拟的瞬时写入故障")
        return await self._inner.add(content, meta)

    async def search(self, query, k=5):
        return await self._inner.search(query, k=k)

    async def consolidate(self):
        return await self._inner.consolidate()


# ---------------------------------------------------------------- 并发对话

async def test_concurrent_chats_with_background_writes(tmp_path):
    """N 路并发对话(后台异步写)+ 并发检索:无异常、drain 后全部可查。"""
    cfg = make_fake_config(tmp_path)
    store, db, _ = build_store(cfg)
    agent = MemoryAgent(EchoMemoryLLM(), store, cfg)

    projects = ["星辰", "苍穹", "北斗", "沧海", "昆仑", "祝融",
                "夸父", "玄武", "麒麟", "鸿蒙", "烛龙", "貔貅"]
    people = ["李雷", "韩梅", "王强", "赵敏", "陈默", "周航",
              "吴迪", "郑爽", "冯远", "褚健", "卫东", "蒋琴"]
    n = len(projects)
    responses = await asyncio.gather(*[
        agent.chat(f"事实:项目{projects[i]}的负责人是{people[i]}", session_id=f"s{i}")
        for i in range(n)
    ])
    assert len(responses) == n and all(r.reply for r in responses)

    await agent.drain()
    assert not agent.last_write_error
    assert await db.count() == n  # 每路对话写入一条记忆

    # 写入完成后并发检索,每路都能命中自己的事实
    hit_lists = await asyncio.gather(*[
        store.search(MultimodalInput.text(f"项目{projects[i]}的负责人是谁"), k=3)
        for i in range(n)
    ])
    for i, hits in enumerate(hit_lists):
        assert hits and people[i] in hits[0].content


async def test_background_write_failure_is_isolated(tmp_path):
    """单次后台写失败:不影响对话返回、不炸事件循环、其余写入完好、可观测。"""
    cfg = make_fake_config(tmp_path)
    store, db, _ = build_store(cfg)
    flaky = FlakyStore(store, fail_on=2)  # 第二次 add 失败
    agent = MemoryAgent(EchoMemoryLLM(), flaky, cfg)

    responses = await asyncio.gather(*[
        agent.chat(f"消息{i}", session_id="s") for i in range(4)
    ])
    assert len(responses) == 4  # 对话全部正常返回

    await agent.drain()  # 不得抛异常
    assert agent.last_write_error, "后台写失败必须可观测"
    assert await db.count() == 3  # 4 次写入,1 次失败


# ---------------------------------------------------------------- 共享池多实例

async def test_two_instances_share_one_pool(tmp_path):
    """两个 agent 实例挂同一共享池:A 上交的记忆 B 立即可检索,反之亦然;
    双方的私有记忆互不可见。"""
    cfg = make_fake_config(tmp_path)
    embedder = build_embedder(cfg.embedder)
    # 同一底层存储上的两个"实例":各自私有 collection,共享同一 shared collection
    db_a = QdrantAdapter(cfg.vectordb, dim=64, collection="private_a")
    db_b = QdrantAdapter(cfg.vectordb, dim=64, collection="private_b", share_client_from=db_a)
    shared = QdrantAdapter(cfg.vectordb, dim=64, collection="pool", share_client_from=db_a)
    store_a = QdrantMemoryStore(embedder, ScriptedLLM(), db_a, cfg, shared_db=shared)
    store_b = QdrantMemoryStore(embedder, ScriptedLLM(), db_b, cfg, shared_db=shared)

    priv_a = await store_a.add(MultimodalInput.text("A 的私有记忆:内部密钥位置"), {})
    await store_b.add(MultimodalInput.text("B 的私有记忆:个人日程"), {})
    await store_a.promote(priv_a)
    await store_b.add(MultimodalInput.text("B 直接共享:巴黎是法国首都"), {"visibility": "shared"})

    # B 能看到 A 上交的;A 能看到 B 共享的
    hits_b = await store_b.search(MultimodalInput.text("内部密钥位置"), k=5)
    assert any("A 的私有记忆" in h.content and h.meta.get("visibility") == "shared" for h in hits_b)
    hits_a = await store_a.search(MultimodalInput.text("巴黎是法国首都"), k=5)
    assert any("B 直接共享" in h.content for h in hits_a)
    # 私有互不可见
    assert not any("个人日程" in h.content for h in await store_a.search(
        MultimodalInput.text("B 的私有记忆 个人日程"), k=10))
    assert not any("内部密钥" in h.content and h.meta.get("visibility") == "private"
                   for h in hits_b)


async def test_concurrent_promotes_to_shared_pool(tmp_path):
    """并发上交 N 条记忆:共享池条数精确,无丢失无重复。"""
    cfg = make_fake_config(tmp_path)
    store, _, shared = build_store(cfg, with_shared=True)
    ids = []
    for i in range(10):
        ids.append(await store.add(MultimodalInput.text(f"客观事实{i}:常量 C{i} 的值为 {i}"), {}))
    shared_ids = await asyncio.gather(*[store.promote(mid) for mid in ids])
    assert len(set(shared_ids)) == 10
    assert await shared.count() == 10


# ---------------------------------------------------------------- 埋点并发

async def test_concurrent_retrieval_logging(tmp_path):
    """并发对话下事件日志逐行完好,event_id 与响应一一对应。"""
    cfg = make_fake_config(tmp_path)
    store, _, _ = build_store(cfg)
    agent = MemoryAgent(EchoMemoryLLM(), store, cfg)
    logger = RetrievalLogger(cfg.metabolism.events_path)
    agent.set_retrieval_logger(logger)

    responses = await asyncio.gather(*[
        agent.chat(f"问题{i}", session_id="s", sync_memory_write=True) for i in range(16)
    ])
    events = logger.load_events()
    assert len(events) == 16
    assert {e.event_id for e in events} == {r.event_id for r in responses}


# ---------------------------------------------------------------- 其他并发面

async def test_consolidate_survives_concurrent_adds(tmp_path):
    """整合与持续写入并发:两者都不抛异常,库保持可用。"""
    cfg = make_fake_config(tmp_path)
    store, db, _ = build_store(cfg)
    for i in range(6):
        await store.add(MultimodalInput.text(f"记录{i}:今日例行琐事流水{i}"), {})

    async def keep_adding():
        for i in range(6):
            await store.add(MultimodalInput.text(f"新增{i}:另一批完全不同的内容{i}"), {})

    report, _ = await asyncio.gather(store.consolidate(), keep_adding())
    assert report.total_before >= 6
    hits = await store.search(MultimodalInput.text("另一批完全不同的内容"), k=3)
    assert hits  # 库仍可用


def test_uuid7_burst_uniqueness_and_ordering():
    """突发生成 20000 个 uuid7:全部唯一,且时间前缀总体非降。"""
    import uuid as uuid_mod

    ids = [uuid7() for _ in range(20000)]
    assert len(set(ids)) == 20000
    ts = [uuid_mod.UUID(u).int >> 80 for u in ids]
    assert ts == sorted(ts)  # 同毫秒内前缀相同,跨毫秒单调不降


async def test_concurrent_first_use_creates_collection_once(tmp_path):
    """ensure_collection 竞态:exists→create 之间的 await 窗口(server 模式网络往返)
    会让并发首用双双判定 not-exists 并重复建集合,后者报 "already exists"。
    加锁后 N 路并发首用全部成功且集合只建一次。"""
    from adapters.vectordb import QdrantAdapter

    class SlowFakeClient:
        """模拟 server 端:每次调用让出事件循环;重复建集合同真实 client 一样报错。"""

        def __init__(self) -> None:
            self.collections: dict[str, bool] = {}

        async def collection_exists(self, name):
            await asyncio.sleep(0)  # 网络往返让出 → 竞态窗口
            return name in self.collections

        async def create_collection(self, collection_name, vectors_config):
            await asyncio.sleep(0)
            if collection_name in self.collections:
                raise ValueError(f"Collection {collection_name} already exists")
            self.collections[collection_name] = True

        async def get_collection(self, name):
            class _Vec:
                size = 8

            class _Params:
                vectors = _Vec()

            class _Cfg:
                params = _Params()

            class _Info:
                config = _Cfg()

            await asyncio.sleep(0)
            return _Info()

    cfg = make_fake_config(tmp_path)
    db = QdrantAdapter(cfg.vectordb, dim=8)
    db._client = SlowFakeClient()  # 替换底层 client,仅测 ensure_collection 并发语义

    results = await asyncio.gather(*[db.ensure_collection() for _ in range(8)],
                                   return_exceptions=True)
    assert not [r for r in results if isinstance(r, Exception)]
    assert list(db._client.collections) == [db.collection]  # 只建一次


async def test_llm_semaphore_covers_tools_and_stream():
    """ConcurrencyLimitedLLM:chat_tools / chat_stream 必须同样计入信号量。

    回归:此前只有 chat 被限流,工具循环(默认 autonomy=tools)与流式调用经
    __getattr__ 透传绕过信号量;且不支持工具/流式的后端不得被误判为支持。
    """
    from types import SimpleNamespace

    from adapters.llm import ConcurrencyLimitedLLM

    class ProbeLLM:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0

        async def _track(self, result):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return result

        async def chat(self, messages, **kw):
            return await self._track("ok")

        async def chat_tools(self, messages, tools=None, **kw):
            return await self._track(SimpleNamespace(content="ok", tool_calls=[]))

        async def chat_stream(self, messages, **kw):
            yield await self._track("chunk")

        async def health(self):
            return True

    inner = ProbeLLM()
    limited = ConcurrencyLimitedLLM(inner, 2)

    async def via_tools():
        return await limited.chat_tools([], tools=[])

    async def via_stream():
        return [c async for c in limited.chat_stream([])]

    await asyncio.gather(*[via_tools() for _ in range(4)], *[via_stream() for _ in range(4)])
    assert inner.max_active <= 2            # 工具/流式调用同样受限
    assert await limited.health()           # 透传属性仍可用

    class PlainLLM:
        async def chat(self, messages, **kw):
            return "ok"

    plain = ConcurrencyLimitedLLM(PlainLLM(), 2)
    assert not hasattr(plain, "chat_tools")   # 无工具能力的后端不得被误判
    assert not hasattr(plain, "chat_stream")
