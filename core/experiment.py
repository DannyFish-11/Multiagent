"""实验运行器 ExperimentRunner(PHASE4 M14.2)。

实验 = 一份声明式 YAML:
  experiment_id, population(N + 各实例 policy/参数差异), task_source(synthetic|replay),
  stop(时长/任务数/预算), metrics(采集列表), random_seed, model_tier(实验变量)

能力:一键启动种群、注入任务流、定期快照、结束出数据包(CSV + 元数据 + 指纹)。
可复现性:同 YAML + 同 seed → 任务序列/注入时点/评估口径逐位一致(LLM 输出本身
不可复现,但流程级一致);数据包记录全部模型/版本/config 指纹。

预算:实验启动前必须声明预算(stop.budget_usd),经 CostLedger.experiment_id 独立
累计,烧穿即暂停(状态可恢复续跑),而非杀实例。
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

from core.plugins import get_plugin, register


# ---------------------------------------------------------------- 任务源

@dataclass
class Task:
    task_id: str
    kind: str                       # 任务类型(检索/整合/查证/计算…)
    payload: dict
    truth: dict = field(default_factory=dict)  # 真值标签(仅框架可见,agent 不可见)


class SyntheticTaskSource:
    """合成任务集(JSONL)。每行 {task_id?, kind, payload, truth}。"""

    def __init__(self, path: str | Path, seed: int, limit: int | None = None) -> None:
        self._rows = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines()
                      if l.strip()]
        self._seed = seed
        self._limit = limit

    def stream(self) -> list[Task]:
        rng = random.Random(self._seed)          # 同 seed → 注入序列逐位一致
        rows = list(self._rows)
        rng.shuffle(rows)
        if self._limit is not None:
            rows = rows[: self._limit]
        return [Task(task_id=r.get("task_id", f"t{i}"), kind=r.get("kind", "generic"),
                     payload=r.get("payload", {}), truth=r.get("truth", {}))
                for i, r in enumerate(rows)]


class ReplayTaskSource:
    """回放真实使用日志(retrieval_events.jsonl),脱敏开关。"""

    def __init__(self, path: str | Path, seed: int, redact: bool = True,
                 limit: int | None = None) -> None:
        self._path = Path(path)
        self._seed = seed
        self._redact = redact
        self._limit = limit

    def stream(self) -> list[Task]:
        if not self._path.exists():
            return []
        rows = [json.loads(l) for l in self._path.read_text(encoding="utf-8").splitlines()
                if l.strip() and json.loads(l).get("kind") != "feedback"]
        rng = random.Random(self._seed)
        rng.shuffle(rows)
        if self._limit is not None:
            rows = rows[: self._limit]
        tasks = []
        for i, r in enumerate(rows):
            query = r.get("query", "")
            if self._redact:
                query = _redact(query)
            tasks.append(Task(task_id=r.get("event_id", f"r{i}"), kind="replay",
                              payload={"query": query},
                              truth={"adopted_ids": r.get("adopted_ids", [])}))
        return tasks


def _redact(text: str) -> str:
    import re

    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<email>", text)
    text = re.sub(r"\b\d{11,}\b", "<number>", text)
    return text


register("task_source", "synthetic")(
    lambda spec, seed: SyntheticTaskSource(spec["path"], seed, spec.get("limit")))
register("task_source", "replay")(
    lambda spec, seed: ReplayTaskSource(spec["path"], seed, spec.get("redact", True),
                                        spec.get("limit")))


@register("task_source", "inspect")
def _ts_inspect(spec, seed):
    # 名字常驻;InspectTaskSource 及 inspect_ai 仅在真正用到时 lazy import
    from adapters.task_source_inspect import InspectTaskSource

    return InspectTaskSource(spec, seed)


def make_task_source(spec: dict, seed: int) -> Any:
    """按 spec['type'] 从插件表取任务源(synthetic/replay/inspect/第三方)。

    第三方基准/数据集(GitHub 上的 benchmark)做成一个 task_source 插件掉进来即可跑。
    """
    return get_plugin("task_source", spec.get("type", "synthetic"))(spec, seed)


# ---------------------------------------------------------------- 预算暂停

class BudgetExhausted(RuntimeError):
    def __init__(self, experiment_id: str, spent: float, budget: float) -> None:
        super().__init__(f"实验 {experiment_id} 预算烧穿:已花 ${spent:.4f} >= ${budget:.4f},暂停")
        self.spent = spent
        self.budget = budget


# ---------------------------------------------------------------- 运行器

@dataclass
class ExperimentConfig:
    experiment_id: str
    population: list[dict]           # 每实例的 policy/参数差异
    task_source: dict
    stop: dict                       # {max_tasks, max_seconds, budget_usd}
    metrics: list[str]
    random_seed: int = 0
    model_tier: str = "cheap"        # 实验变量,切换零成本

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ExperimentConfig":
        d = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls(
            experiment_id=d["experiment_id"], population=d["population"],
            task_source=d["task_source"], stop=d.get("stop", {}),
            metrics=d.get("metrics", []), random_seed=int(d.get("random_seed", 0)),
            model_tier=d.get("model_tier", "cheap"))

    def fingerprint(self) -> str:
        blob = json.dumps({
            "experiment_id": self.experiment_id, "population": self.population,
            "task_source": self.task_source, "stop": self.stop,
            "seed": self.random_seed, "model_tier": self.model_tier,
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


# 任务处理器:(instance, task) -> 结果 dict(含 metrics 字段)。由实验脚本注入。
TaskHandler = Callable[[dict, Task], Awaitable[dict]]


class ExperimentRunner:
    def __init__(self, config: ExperimentConfig, out_dir: str | Path,
                 ledger=None, model_versions: dict | None = None) -> None:
        self._cfg = config
        self._out = Path(out_dir) / config.experiment_id
        self._out.mkdir(parents=True, exist_ok=True)
        self._ledger = ledger
        self._model_versions = model_versions or {}
        self._records: list[dict] = []
        self._paused = False
        self._state_path = self._out / "state.json"

    def _budget_ok(self) -> bool:
        budget = self._cfg.stop.get("budget_usd")
        if budget is None or self._ledger is None:
            return True
        return self._ledger.experiment_usd(self._cfg.experiment_id) < budget

    async def run(self, handler: TaskHandler, assign: Callable[[Task, list[dict]], dict] | None = None) -> dict:
        """跑一次实验。assign 决定某任务派给哪个实例(默认轮转);烧穿预算即暂停并可续跑。"""
        source = make_task_source(self._cfg.task_source, self._cfg.random_seed)
        tasks = source.stream()
        max_tasks = self._cfg.stop.get("max_tasks")
        if max_tasks is not None:
            tasks = tasks[:max_tasks]
        deadline = time.time() + self._cfg.stop["max_seconds"] if "max_seconds" in self._cfg.stop else None

        done_ids = self._load_done()  # 续跑:跳过已完成任务
        instances = list(self._cfg.population)

        for idx, task in enumerate(tasks):
            if task.task_id in done_ids:
                continue
            if not self._budget_ok():
                self._paused = True
                self._save_state(idx)
                raise BudgetExhausted(self._cfg.experiment_id,
                                      self._ledger.experiment_usd(self._cfg.experiment_id),
                                      self._cfg.stop["budget_usd"])
            if deadline is not None and time.time() >= deadline:
                break
            inst = (assign(task, instances) if assign
                    else instances[idx % len(instances)])
            result = await handler(inst, task)
            record = {
                "task_id": task.task_id, "kind": task.kind,
                "agent_id": inst.get("agent_id", ""), **result}
            self._records.append(record)
            self._append_record(record)
            done_ids.add(task.task_id)
            self._append_done(task.task_id)

        return self._finalize()

    def _all_records(self) -> list[dict]:
        """全部历史记录:results.jsonl 逐任务落盘,续跑(新实例)时前期记录不丢。"""
        p = self._out / "results.jsonl"
        if not p.exists():
            return list(self._records)
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    def _finalize(self) -> dict:
        records = self._all_records()
        csv_path = self._out / "results.csv"
        fields: list[str] = []
        for r in records:
            for k in r:
                if k not in fields:
                    fields.append(k)
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(records)
        meta = {
            "experiment_id": self._cfg.experiment_id,
            "fingerprint": self._cfg.fingerprint(),
            "seed": self._cfg.random_seed,
            "model_tier": self._cfg.model_tier,
            "model_versions": self._model_versions,
            "population_size": len(self._cfg.population),
            "tasks_completed": len(records),
            "metrics_requested": self._cfg.metrics,
            "paused": self._paused,
            "experiment_usd": (self._ledger.experiment_usd(self._cfg.experiment_id)
                               if self._ledger else 0.0),
        }
        (self._out / "metadata.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"csv": str(csv_path), "metadata": meta}

    def snapshot(self, commons_state: dict | None = None, audit_entries: list | None = None,
                 memory_packs: dict[str, str] | None = None) -> str:
        """定期快照:各实例记忆库(MemoryPack 路径引用)+ commons + 审计,打包为 JSON。"""
        snap = {
            "ts": time.time(), "experiment_id": self._cfg.experiment_id,
            "commons": commons_state or {}, "audit_tail": (audit_entries or [])[-200:],
            "memory_packs": memory_packs or {},
        }
        path = self._out / f"snapshot-{len(list(self._out.glob('snapshot-*.json')))}.json"
        path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)

    # ---- 续跑状态 ----

    def _load_done(self) -> set[str]:
        p = self._out / "done.jsonl"
        if not p.exists():
            return set()
        return {json.loads(l)["task_id"] for l in p.read_text(encoding="utf-8").splitlines() if l.strip()}

    def _append_done(self, task_id: str) -> None:
        with (self._out / "done.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"task_id": task_id}) + "\n")

    def _append_record(self, record: dict) -> None:
        with (self._out / "results.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _save_state(self, next_index: int) -> None:
        self._state_path.write_text(json.dumps({
            "paused": True, "next_index": next_index, "ts": time.time()}), encoding="utf-8")
