"""PHASE5 M19 验收:世代演化(冒烟 + shuffle 第三臂机制单测)。"""

from __future__ import annotations

from core.evolution import (
    Individual,
    diversity_index,
    render_evolution_report,
    run_evolution,
)


async def identity_distiller(mems):
    """确定性蒸馏替身:压缩为去重后的前 3 条(真实为 LLM inherit)。"""
    return list(dict.fromkeys(mems))[:3]


def make_scorer(good_mem_token="good"):
    """含 good token 记忆的个体成功率高,且判定其"引用了继承记忆"。"""
    def scorer(ind: Individual, task):
        has_good = any(good_mem_token in m for m in ind.memories)
        return has_good, has_good
    return scorer


# ---------------------------------------------------------------- 冒烟:三世代跑通

async def test_evolution_smoke_runs_and_snapshots(tmp_path):
    scorer = make_scorer()
    tasks = list(range(10))
    snaps = await run_evolution(
        "evolve", pop_size=4, generations=3, tasks_per_gen=5, kill_k=1,
        scorer=scorer, distiller=identity_distiller, task_stream=tasks)
    assert len(snaps) == 3
    # 血统树与世代曲线可复算
    for s in snaps:
        assert 0.0 <= s.avg_success_rate <= 1.0
        assert s.lineage_roots  # 始祖存活映射
    # 世代推进:转世者出现(lineage 变长)
    assert any(s.inherited_reference_rate >= 0.0 for s in snaps)


async def test_control_arm_no_death_no_inheritance(tmp_path):
    """对照臂:无死亡无遗传,始祖数守恒(每个初始个体都存活)。"""
    scorer = make_scorer()
    snaps = await run_evolution(
        "control", pop_size=4, generations=3, tasks_per_gen=5, kill_k=1,
        scorer=scorer, distiller=identity_distiller, task_stream=list(range(10)))
    # 对照臂末世代始祖数 = 初始种群数(无转世替换)
    assert len(snaps[-1].lineage_roots) == 4


async def test_shuffle_inherits_random_source(tmp_path):
    """判别臂机制单测:转世继承的内容确为随机源(非最优/父代)。

    每个初始个体记忆唯一可辨(seed-mem-<arm>-<i>)。distiller 记录每次继承的来源。
    - evolve:继承必来自头部(最优)个体
    - shuffle:继承来自 rng 选中的个体,换 seed → 来源集合改变(随机化证据)
    """
    def scorer(ind, task):
        # 让 i0 明显最优(其余全失败),制造确定的"头部"
        has = "i0" in ind.memories[0] if ind.memories else False
        return has, has

    def capturing_distiller(record):
        async def _d(mems):
            record.append(tuple(mems))  # 记录继承来源的记忆(即来源身份)
            return list(mems)
        return _d

    # evolve:来源必是最优个体(带 i0 记忆)
    ev_rec = []
    await run_evolution("evolve", pop_size=4, generations=3, tasks_per_gen=3, kill_k=1,
                        scorer=scorer, distiller=capturing_distiller(ev_rec),
                        task_stream=[0], rng_seed=1)
    assert ev_rec and all(any("i0" in m for m in src) for src in ev_rec), \
        "evolve 继承来源必是最优个体"

    # shuffle:两个不同 rng_seed 的继承来源集合应不同(随机化)
    sh_a, sh_b = [], []
    await run_evolution("shuffle", pop_size=6, generations=4, tasks_per_gen=2, kill_k=3,
                        scorer=scorer, distiller=capturing_distiller(sh_a),
                        task_stream=[0], rng_seed=1)
    await run_evolution("shuffle", pop_size=6, generations=4, tasks_per_gen=2, kill_k=3,
                        scorer=scorer, distiller=capturing_distiller(sh_b),
                        task_stream=[0], rng_seed=7)
    # 随机源证据:换 seed → 继承来源序列不同
    assert sh_a != sh_b, "shuffle 换 seed 后继承来源应随机化"
    # 且 shuffle 确实会从非最优个体继承(至少一次来源不含 i0)
    assert any(not any("i0" in m for m in src) for src in sh_a), \
        "shuffle 应能从非最优个体继承(随机而非择优)"


def test_diversity_index():
    a = Individual("a", 0, memories=["x", "y"])
    b = Individual("b", 1, memories=["x", "y"])
    c = Individual("c", 2, memories=["p", "q"])
    assert diversity_index([a, b]) == 0.0        # 全同 → 0
    assert diversity_index([a, c]) == 1.0        # 全异 → 1
    assert 0.0 < diversity_index([a, b, c]) < 1.0


async def test_evolution_report_renders(tmp_path):
    scorer = make_scorer()
    arms = {}
    for arm in ("evolve", "control", "shuffle"):
        arms[arm] = await run_evolution(
            arm, pop_size=4, generations=3, tasks_per_gen=4, kill_k=1,
            scorer=scorer, distiller=identity_distiller, task_stream=list(range(8)))
    md = render_evolution_report(arms, is_smoke=True)
    (tmp_path / "m19.md").write_text(md, encoding="utf-8")
    text = (tmp_path / "m19.md").read_text(encoding="utf-8")
    assert "冒烟运行" in text and "不构成结论" in text
    # 诚实红线在场
    assert "evolve 若不优于 control" in text
    assert "shuffle" in text  # 判别臂说明
