"""世代演化实验(PHASE5 M19)——记忆遗传的兑现。

机制全部已有:M7 inherit(蒸馏遗传)+ M5 lineage + M16 任务竞争/成败反馈。
本模块把它们接成演化循环并观测。三臂:
  evolve  :死亡 + 遗传(末位死亡,头部经 inherit 蒸馏转世,lineage 记血统)
  control :同规模同任务流,无死亡无遗传(分离"遗传选择"与"单纯多跑任务")
  shuffle :转世但继承随机实例的记忆(判别"遗传有效"vs"仅淘汰坏运气实例")

诚实红线:若演化臂不优于对照臂,如实报告。shuffle 臂的"继承内容确为随机源"经单测验证。

为可测,实例抽象为 Individual(持有记忆条目列表 + 累计成功记录);inherit 用可注入
的蒸馏函数(真实为 LLM,测试为确定性替身)。真实上云时接 MemoryPack.inherit。
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class Individual:
    agent_id: str
    seed: int
    memories: list[str] = field(default_factory=list)   # 继承/习得的经验条目
    lineage: list[str] = field(default_factory=list)     # 血统(始祖 agent_id 链)
    successes: int = 0
    attempts: int = 0

    @property
    def success_rate(self) -> float:
        return self.successes / self.attempts if self.attempts else 0.0


# 蒸馏遗传:(源个体记忆) -> 转世者初始记忆。真实为 LLM inherit;测试为确定性。
Distiller = Callable[[list[str]], Awaitable[list[str]]]
# 任务判分:(个体, 任务) -> (成功?, 是否引用了继承记忆)
TaskScorer = Callable[[Individual, Any], tuple[bool, bool]]


@dataclass
class GenerationSnapshot:
    generation: int
    avg_success_rate: float
    diversity: float
    lineage_roots: dict[str, int]          # 始祖 -> 存活后代数
    inherited_reference_rate: float         # 转世者引用继承记忆的比率


def diversity_index(pop: list[Individual]) -> float:
    """种群记忆库间差异度(0=全同,1=全异)。用两两 Jaccard 距离均值。"""
    sets = [set(i.memories) for i in pop]
    if len(sets) < 2:
        return 0.0
    dists = []
    for a in range(len(sets)):
        for b in range(a + 1, len(sets)):
            union = sets[a] | sets[b]
            inter = sets[a] & sets[b]
            jac = len(inter) / len(union) if union else 1.0
            dists.append(1.0 - jac)
    return statistics.mean(dists) if dists else 0.0


async def run_evolution(
    arm: str, pop_size: int, generations: int, tasks_per_gen: int,
    kill_k: int, scorer: TaskScorer, distiller: Distiller,
    task_stream: list[Any], base_seed: int = 0,
    rng_seed: int = 0,
) -> list[GenerationSnapshot]:
    """跑一个演化臂。arm ∈ {evolve, control, shuffle}。返回每世代快照。"""
    rng = random.Random(rng_seed)
    pop = [Individual(agent_id=f"{arm}-g0-i{i}", seed=base_seed + i,
                      memories=[f"seed-mem-{arm}-i{i}"], lineage=[f"{arm}-g0-i{i}"])
           for i in range(pop_size)]
    snapshots: list[GenerationSnapshot] = []

    for gen in range(generations):
        # 一个世代:每个个体跑 tasks_per_gen 个任务
        inherited_refs = 0
        inherited_uses = 0
        for ind in pop:
            for t in range(tasks_per_gen):
                task = task_stream[(gen * tasks_per_gen + t) % len(task_stream)]
                ok, used_inherited = scorer(ind, task)
                ind.attempts += 1
                if ok:
                    ind.successes += 1
                if ind.lineage[:-1]:  # 是转世者(有祖先)
                    inherited_refs += 1
                    if used_inherited:
                        inherited_uses += 1

        avg = statistics.mean(i.success_rate for i in pop)
        roots: dict[str, int] = {}
        for ind in pop:
            roots[ind.lineage[0]] = roots.get(ind.lineage[0], 0) + 1
        snapshots.append(GenerationSnapshot(
            generation=gen, avg_success_rate=round(avg, 4),
            diversity=round(diversity_index(pop), 4), lineage_roots=roots,
            inherited_reference_rate=round(inherited_uses / inherited_refs, 4) if inherited_refs else 0.0))

        if gen == generations - 1:
            break

        # 世代交替
        if arm == "control":
            # 对照:无死亡无遗传,个体保留(继续累计)
            continue

        ranked = sorted(pop, key=lambda i: i.success_rate, reverse=True)
        survivors = ranked[:pop_size - kill_k]
        dying = ranked[pop_size - kill_k:]
        top = ranked[:max(1, kill_k)]  # 头部作为转世来源

        newborns: list[Individual] = []
        for j, _dead in enumerate(dying):
            if arm == "evolve":
                parent = top[j % len(top)]
                inherited = await distiller(parent.memories)
                lineage = list(parent.lineage) + [f"{arm}-g{gen+1}-i{j}"]
            else:  # shuffle:转世但继承随机实例的记忆(判别用)
                donor = rng.choice(pop)
                inherited = await distiller(donor.memories)
                lineage = list(donor.lineage) + [f"{arm}-g{gen+1}-i{j}"]
            newborns.append(Individual(
                agent_id=f"{arm}-g{gen+1}-i{j}", seed=base_seed + 1000 * (gen + 1) + j,
                memories=inherited, lineage=lineage))
        # 幸存者累计清零进入下一世代(每世代独立评估),继承者从零
        for s in survivors:
            s.successes = 0
            s.attempts = 0
        pop = survivors + newborns

    return snapshots


def render_evolution_report(arms: dict[str, list[GenerationSnapshot]], is_smoke: bool) -> str:
    lines = ["# M19 世代演化实验:遗传是否抬升群体", ""]
    if is_smoke:
        lines += ["> ⚠️ **冒烟运行**:小种群 × 少世代,验证血统树/世代曲线可复算,**不构成结论**。",
                  "> 满配需 M18 全部安全带就位 + 人类授权预算后上云跑。", ""]
    lines += ["## 世代平均成功率曲线", ""]
    for arm, snaps in arms.items():
        curve = [s.avg_success_rate for s in snaps]
        lines.append(f"- **{arm}**:{curve}")
    lines += ["", "## 遗传记忆引用率(转世者真在用继承经验吗)", ""]
    for arm, snaps in arms.items():
        refs = [s.inherited_reference_rate for s in snaps]
        lines.append(f"- **{arm}**:{refs}")
    lines += ["", "## 多样性(警惕早熟收敛)", ""]
    for arm, snaps in arms.items():
        div = [s.diversity for s in snaps]
        lines.append(f"- **{arm}**:{div}")
    lines += ["", "## 血统树(末世代始祖存活)", ""]
    for arm, snaps in arms.items():
        lines.append(f"- **{arm}**:{snaps[-1].lineage_roots}")
    lines += ["", "## 诚实红线", "- evolve 若不优于 control → 遗传无效,如实报告;",
              "- evolve vs shuffle 判别'遗传有效'与'仅淘汰坏运气':shuffle 继承随机源,",
              "  若 evolve ≈ shuffle 则优势来自淘汰而非遗传内容。", ""]
    return "\n".join(lines)
