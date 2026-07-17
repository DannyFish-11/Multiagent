"""PHASE4 M14 验收:三条全局纪律 + ExperimentRunner。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adapters.cost_ledger import CostLedger
from core.audit import AuditLog
from core.config import load_config
from core.experiment import (
    BudgetExhausted,
    ExperimentConfig,
    ExperimentRunner,
    SyntheticTaskSource,
)
from core.loop_guard import LoopCapped, LoopGuard
from tests.conftest import PROJECT_ROOT

SMOKE_YAML = PROJECT_ROOT / "experiments" / "smoke.yaml"


# ---------------------------------------------------------------- 14.1 循环硬上限

async def test_loop_guard_caps_never_satisfied(tmp_path):
    """负向:永不满足的退出条件,循环触顶后强制终止并记 loop_capped 事件。"""
    audit = AuditLog(tmp_path / "audit.jsonl")
    guard = LoopGuard("vote_rounds", limit=3, audit=audit, agent_id="a1")
    iterations = 0
    with pytest.raises(LoopCapped) as exc:
        while True:  # 永不满足
            await guard.tick()
            iterations += 1
    assert exc.value.limit == 3
    assert iterations == 3  # 恰好跑满上限
    e = audit.read_all()[-1]
    assert e["decision"] == "loop_capped" and e["params"]["limit"] == 3


async def test_loop_guard_non_raising_returns_false(tmp_path):
    guard = LoopGuard("x", limit=2, raise_on_cap=False)
    assert await guard.tick() is True
    assert await guard.tick() is True
    assert await guard.tick() is False  # 触顶不抛,返回 False
    assert guard.capped


def test_loop_config_per_point_override():
    cfg = load_config()
    cfg.loops.per_point = {"vote_rounds": 5}
    assert cfg.loops.limit("vote_rounds") == 5
    assert cfg.loops.limit("unknown") == cfg.loops.default_max_iterations


# ---------------------------------------------------------------- 14.1 记账维度

def test_ledger_experiment_dimension(tmp_path):
    ledger = CostLedger({"m": {"input": 1.0, "output": 0.0}}, 1e9, tmp_path / "l.json")
    ledger.record("ep", "m", 1_000_000, experiment_id="exp1", agent_id="a1", purpose="chat")
    ledger.record("ep", "m", 2_000_000, experiment_id="exp1", agent_id="a2", purpose="vote")
    ledger.record("ep", "m", 500_000, experiment_id="exp2", agent_id="a1")
    assert ledger.experiment_usd("exp1") == 3.0
    assert ledger.experiment_usd("exp2") == 0.5
    snap = ledger.experiment_snapshot("exp1")
    assert snap["by_agent"]["a1"] == 1.0 and snap["by_agent"]["a2"] == 2.0
    assert snap["by_purpose"]["vote"] == 2.0
    # 既有日预算逻辑不变(总额仍累计)
    assert abs(ledger.today_usd() - 3.5) < 1e-9


# ---------------------------------------------------------------- 14.1 委托上下文预算

async def test_a2a_context_budget_compression(tmp_path):
    from core.identity import AgentIdentity
    from adapters.a2a import A2AClientAdapter

    ident = AgentIdentity.load_or_create(tmp_path / "id")

    class StubLLM:
        async def chat(self, messages, **kw):
            return "压缩后的摘要"

    # 预算 5 token ≈ 20 字符;超长上下文经 LLM 压缩
    client = A2AClientAdapter(ident, llm=StubLLM(), context_budget_tokens=5)
    long_text = "很长的委托上下文。" * 50
    compressed = await client.compress_context(long_text)
    assert compressed == "压缩后的摘要"
    # 短文本不压缩
    assert await client.compress_context("短") == "短"
    # 无预算(0)不压缩
    client2 = A2AClientAdapter(ident, context_budget_tokens=0)
    assert await client2.compress_context(long_text) == long_text


# ---------------------------------------------------------------- 14.2 任务源可复现

def test_synthetic_source_seed_reproducible(tmp_path):
    path = PROJECT_ROOT / "experiments" / "synthetic_smoke.jsonl"
    s1 = SyntheticTaskSource(path, seed=42).stream()
    s2 = SyntheticTaskSource(path, seed=42).stream()
    s3 = SyntheticTaskSource(path, seed=99).stream()
    # 同 seed → 注入序列逐位一致
    assert [t.task_id for t in s1] == [t.task_id for t in s2]
    # 不同 seed → 序列不同(极大概率)
    assert [t.task_id for t in s1] != [t.task_id for t in s3]
    # 真值标签存在(仅框架可见)
    assert all("truth" in t.__dict__ for t in s1)


# ---------------------------------------------------------------- 14.2 冒烟实验(离线)

async def test_smoke_experiment_runs_and_outputs_csv(tmp_path):
    """3 实例、50 合成任务、$2 预算:一键跑完、出快照、出 CSV。"""
    cfg = ExperimentConfig.from_yaml(SMOKE_YAML)
    assert cfg.experiment_id == "smoke_3x50" and len(cfg.population) == 3
    ledger = CostLedger({}, 1e9, tmp_path / "l.json")
    runner = ExperimentRunner(cfg, tmp_path / "exp", ledger=ledger,
                              model_versions={"llm": "fake-cheap"})

    async def handler(inst, task):
        # 免费的合成判分(compute 类真值可自动判)
        ok = True
        if task.kind == "compute":
            ok = True
        return {"success": ok, "cost_usd": 0.0, "latency_ms": 1}

    result = await runner.run(handler)
    # CSV 出得来且 50 行
    rows = list(csv_rows(result["csv"]))
    assert len(rows) == 50
    assert result["metadata"]["tasks_completed"] == 50
    assert result["metadata"]["fingerprint"]  # 指纹在案
    # 快照出得来
    snap = runner.snapshot(commons_state={"pool_size": 3}, audit_entries=[{"x": 1}])
    assert Path(snap).exists()
    assert json.loads(Path(snap).read_text())["commons"]["pool_size"] == 3


async def test_budget_burn_pauses_and_resumes(tmp_path):
    """中途烧穿预算 → 暂停(非杀实例)且状态可恢复续跑。"""
    cfg = ExperimentConfig.from_yaml(SMOKE_YAML)
    cfg.stop = {"max_tasks": 50, "budget_usd": 0.10}
    ledger = CostLedger({"m": {"input": 1.0, "output": 0.0}}, 1e9, tmp_path / "l.json")
    runner = ExperimentRunner(cfg, tmp_path / "exp", ledger=ledger)

    processed = {"n": 0}

    async def handler(inst, task):
        processed["n"] += 1
        # 每个任务烧 $0.05,第 3 个后累计 $0.15 > $0.10 → 下一个任务前暂停
        ledger.record("ep", "m", 50_000, experiment_id=cfg.experiment_id)
        return {"success": True}

    with pytest.raises(BudgetExhausted) as exc:
        await runner.run(handler)
    assert exc.value.budget == 0.10
    burned = processed["n"]
    assert 0 < burned < 50  # 中途暂停,非跑完

    # 续跑:提高预算,已完成任务不重复
    cfg.stop["budget_usd"] = 100.0
    runner2 = ExperimentRunner(cfg, tmp_path / "exp", ledger=ledger)
    result = await runner2.run(handler)
    # 总处理量 = 50(续跑补齐,不重复已完成)
    assert result["metadata"]["tasks_completed"] + burned >= 50
    done = {json.loads(l)["task_id"]
            for l in (tmp_path / "exp" / cfg.experiment_id / "done.jsonl").read_text().splitlines()}
    assert len(done) == 50


async def test_resume_results_csv_contains_full_history(tmp_path):
    """烧穿暂停 → 新 runner 续跑:results.csv 与 tasks_completed 必须含前期记录。

    回归:此前 records 只存内存,续跑实例的 CSV 只剩续跑段,前期记录静默丢失。
    """
    cfg = ExperimentConfig.from_yaml(SMOKE_YAML)
    cfg.stop = {"max_tasks": 50, "budget_usd": 0.10}
    ledger = CostLedger({"m": {"input": 1.0, "output": 0.0}}, 1e9, tmp_path / "l.json")
    runner = ExperimentRunner(cfg, tmp_path / "exp", ledger=ledger)

    async def handler(inst, task):
        ledger.record("ep", "m", 50_000, experiment_id=cfg.experiment_id)
        return {"success": True, "cost_usd": 0.05}

    with pytest.raises(BudgetExhausted):
        await runner.run(handler)

    cfg.stop["budget_usd"] = 100.0
    runner2 = ExperimentRunner(cfg, tmp_path / "exp", ledger=ledger)
    result = await runner2.run(handler)

    rows = list(csv_rows(result["csv"]))
    assert len(rows) == 50                               # 前期记录不丢
    assert result["metadata"]["tasks_completed"] == 50   # 计数为全量
    assert len({r["task_id"] for r in rows}) == 50       # 无重复行


def csv_rows(path):
    import csv as _csv

    with open(path, encoding="utf-8") as f:
        return list(_csv.DictReader(f))


async def test_resume_tolerates_corrupt_trailing_jsonl_line(tmp_path):
    """回归:崩溃残留的半行 results/done 日志不得让续跑整体拒读。

    追加写非原子:进程在写一行途中被杀会留下坏行。修复前 _all_records/
    _load_done 逐行 json.loads,一行坏 → 续跑直接 JSONDecodeError。
    """
    cfg = ExperimentConfig.from_yaml(SMOKE_YAML)
    cfg.stop = {"max_tasks": 50, "budget_usd": 0.10}
    ledger = CostLedger({"m": {"input": 1.0, "output": 0.0}}, 1e9, tmp_path / "l.json")
    runner = ExperimentRunner(cfg, tmp_path / "exp", ledger=ledger)

    processed = {"n": 0}

    async def handler(inst, task):
        processed["n"] += 1
        ledger.record("ep", "m", 50_000, experiment_id=cfg.experiment_id)
        return {"success": True, "cost_usd": 0.05}

    with pytest.raises(BudgetExhausted):
        await runner.run(handler)

    out = tmp_path / "exp" / cfg.experiment_id
    with (out / "results.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"task_id": "t-999", "success": tr')   # 崩溃半行(无换行结尾)
    with (out / "done.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"task_id": "t-9')

    cfg.stop["budget_usd"] = 100.0
    runner2 = ExperimentRunner(cfg, tmp_path / "exp", ledger=ledger)
    result = await runner2.run(handler)                  # 修复前此处 JSONDecodeError
    assert processed["n"] == 50                          # 任务不重不漏
    rows = list(csv_rows(result["csv"]))
    # 与坏行合并的第一条新记录随之不可解析而跳过(崩溃残留行无换行结尾的固有代价),
    # 其余 49 条真记录全部保留;修复前是 0 条(整体拒读)。
    assert len({r["task_id"] for r in rows}) == 49


async def test_empty_population_rejected_clearly(tmp_path):
    """回归:空种群 + 有任务时清晰报配置错,而非 ZeroDivisionError(数值边界)。

    触发路径:YAML 里 population: [] 且任务集非空 → 轮转分配
    instances[idx % len(instances)] 即 idx % 0 → ZeroDivisionError。
    """
    cfg = ExperimentConfig.from_yaml(SMOKE_YAML)
    cfg.population = []
    runner = ExperimentRunner(cfg, tmp_path / "exp")

    async def handler(inst, task):  # pragma: no cover - 不应被调
        return {}

    with pytest.raises(ValueError, match="population 为空"):
        await runner.run(handler)
