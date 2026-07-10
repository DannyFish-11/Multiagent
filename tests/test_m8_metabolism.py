"""Milestone 8 验收:代谢循环(PHASE2_SPEC 原文三条)。

①构造已知 "k=3 明显优于 k=10" 的合成日志集,metabolism 能发现并建议 k=3
②应用建议的 config diff 后,回放集命中率提升可复现
③确认 metabolism 无任何写代码/自动应用配置的路径(负向)
"""

from __future__ import annotations

import json
from pathlib import Path

from adapters.embedder import build_embedder
from adapters.memory import QdrantMemoryStore
from adapters.vectordb import QdrantAdapter
from core.metabolism import (
    RetrievalEvent,
    RetrievalLogger,
    replay_hit_rate,
    run_metabolism,
)
from core.schemas import MultimodalInput
from tests.conftest import ScriptedLLM, make_fake_config


def build_store(cfg):
    embedder = build_embedder(cfg.embedder)
    db = QdrantAdapter(cfg.vectordb, dim=cfg.embedder.effective_dim)
    return QdrantMemoryStore(embedder, ScriptedLLM(), db, cfg)


async def synth_k3_beats_k10(store, logger: RetrievalLogger) -> None:
    """合成日志集:所有 ground truth 记忆都稳进 top-3。

    注:top-3 ⊆ top-10,命中率对 k 单调不降,故"k=3 明显优于 k=10"的可判定
    形式是:两者命中率持平(均 1.0)而 k=3 检索代价更低 —— metabolism 的
    择优规则在同分时取更小 k(注入上下文更省)。k 变大能提升命中率的反向
    场景由 test_m8_2 覆盖,证明网格在真实测量而非恒推最小 k。
    """
    # 每个查询的目标记忆与查询词面高度重叠 → fake 嵌入下必进 top-1
    targets = []
    for i in range(6):
        mem_id = await store.add(MultimodalInput.text(f"事实{i}:项目代号星辰{i}号的负责人是李雷{i}"), {})
        targets.append((f"项目代号星辰{i}号的负责人是谁", mem_id))
    # 干扰项:与查询几乎无词面重叠
    for i in range(20):
        await store.add(MultimodalInput.text(f"无关备忘{i}:午后的天气与例行琐事记录{i}"), {})

    for i, (query, mem_id) in enumerate(targets):
        logger.log(RetrievalEvent(query=query, hit_ids=[mem_id],
                                  adopted_ids=[mem_id], feedback="up", event_id=f"e{i}"))


async def test_m8_1_metabolism_recommends_k3_over_k10(tmp_path):
    cfg = make_fake_config(tmp_path)
    store = build_store(cfg)
    logger = RetrievalLogger(cfg.metabolism.events_path)
    await synth_k3_beats_k10(store, logger)

    report = await run_metabolism(
        store, cfg.metabolism.events_path, cfg.metabolism.report_dir,
        current_k=10, k_grid=(3, 10),
    )
    # k=3 与 k=10 命中率持平(top-3⊆top-10 且目标皆在 top-3)→ 择优取更省的 k=3
    assert report["grid_results"]["k=3"] >= report["grid_results"]["k=10"]
    assert report["grid_results"]["k=3"] == 1.0
    assert report["recommended_k"] == 3
    assert "top_k: 3" in report["config_diff"] and "top_k: 10" in report["config_diff"]
    # 报告落盘
    saved = json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))
    assert saved["recommended_k"] == 3


async def test_m8_2_applying_diff_improves_reproducibly(tmp_path):
    """应用建议(人工把 top_k 从 1 调到建议值)后,回放命中率提升且可复现。"""
    cfg = make_fake_config(tmp_path)
    store = build_store(cfg)
    logger = RetrievalLogger(cfg.metabolism.events_path)

    # 构造 k=1 会漏、k=3 能中的事件:一条与查询几乎逐字相同的"疑问记录"
    # 干扰项在词面上压过目标记忆(fake 嵌入按 3-gram 词面重叠排序)
    query = "季度报告 终稿 谁负责"
    await store.add(MultimodalInput.text("疑问记录:季度报告 终稿 谁负责(尚无答案)"), {})
    target = await store.add(MultimodalInput.text("张伟负责编写季度报告的最终版本"), {})
    await store.add(MultimodalInput.text("午后的天气与例行琐事"), {})
    logger.log(RetrievalEvent(query=query, hit_ids=[target], adopted_ids=[target],
                              feedback="up", event_id="ev-k"))
    events = logger.load_events()

    rate_before = await replay_hit_rate(store, events, k=1)
    report = await run_metabolism(store, cfg.metabolism.events_path,
                                  cfg.metabolism.report_dir, current_k=1, k_grid=(1, 3, 5))
    k_new = report["recommended_k"]
    assert k_new > 1
    rate_after_1 = await replay_hit_rate(store, events, k=k_new)
    rate_after_2 = await replay_hit_rate(store, events, k=k_new)  # 可复现
    assert rate_after_1 > rate_before
    assert rate_after_1 == rate_after_2 == report["grid_results"][f"k={k_new}"]


async def test_m8_3_no_selfmodify_paths(tmp_path):
    """负向:metabolism 只产出报告文件;不写代码、不改 config,亦无此类代码路径。"""
    import core.metabolism as metabolism_mod

    cfg = make_fake_config(tmp_path)
    store = build_store(cfg)
    RetrievalLogger(cfg.metabolism.events_path).log(
        RetrievalEvent(query="q", hit_ids=["x"], adopted_ids=["x"], event_id="e0"))

    project_root = Path(metabolism_mod.__file__).resolve().parent.parent
    code_and_config_mtimes = {
        p: p.stat().st_mtime
        for p in list(project_root.rglob("*.py")) + [project_root / "config.yaml"]
        if p.exists() and ".venv" not in p.parts
    }

    report = await run_metabolism(store, cfg.metabolism.events_path,
                                  cfg.metabolism.report_dir, current_k=5)

    # 运行后:代码与 config 全部原封不动;新增文件只出现在 report_dir
    for p, mtime in code_and_config_mtimes.items():
        assert p.stat().st_mtime == mtime, f"metabolism 不得触碰 {p}"
    out_files = list(Path(cfg.metabolism.report_dir).glob("*"))
    assert out_files, "报告必须落在 report_dir"
    assert Path(report["report_path"]).parent == Path(cfg.metabolism.report_dir)
    # 源码层面:模块内不存在任何"自动应用"入口
    src = Path(metabolism_mod.__file__).read_text(encoding="utf-8")
    for forbidden in ("def apply", "auto_apply", "config.yaml\", \"w", "yaml.dump"):
        assert forbidden not in src, f"metabolism 源码不得包含自动应用路径: {forbidden}"
    assert "人工审阅" in src  # 边界声明在场


async def test_m8_zero_evidence_gives_no_suggestion(tmp_path):
    """零可用事件(无 adopted/全 down)时不得给出参数建议。"""
    cfg = make_fake_config(tmp_path)
    store = build_store(cfg)
    logger = RetrievalLogger(cfg.metabolism.events_path)
    logger.log(RetrievalEvent(query="q1", hit_ids=["a"], adopted_ids=[], event_id="e1"))
    logger.log(RetrievalEvent(query="q2", hit_ids=["b"], adopted_ids=["b"],
                              feedback="down", event_id="e2"))

    report = await run_metabolism(store, cfg.metabolism.events_path,
                                  cfg.metabolism.report_dir, current_k=5)
    assert report["events_usable"] == 0
    assert report["recommended_k"] == 5  # 维持现状
    assert "不给出建议" in report["config_diff"]
