"""Inspect-AI 任务源(M21):把 UK AISI 的 Inspect-AI 评测/数据集接成实验任务流。

装了可选依赖(`uv sync --extra inspect`)后,实验 YAML 里
``task_source: {type: inspect, task: "mypkg.evals:my_task", limit: 100}`` 即可把任一
Inspect ``Task`` 的数据集喂进 ExperimentRunner——"跑 GitHub 上的 benchmark 做实验"。

也支持直接给数据集文件:``{type: inspect, samples: path/to/samples.jsonl}``(每行
{input, target, id?, metadata?}),不装 inspect_ai 也能用(纯读文件)。

红线:inspect_ai 只在本文件接触(lazy import);产出本项目的 core.experiment.Task 流。
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from core.errors import LayerError
from core.experiment import Task


def _sample_to_task(idx: int, sample) -> Task:
    """把一个 Inspect Sample(或等价 dict)转成本项目 Task。"""
    if isinstance(sample, dict):
        sid = sample.get("id") or f"inspect{idx}"
        inp = sample.get("input", "")
        target = sample.get("target", "")
        meta = sample.get("metadata", {})
    else:  # inspect_ai.dataset.Sample
        sid = getattr(sample, "id", None) or f"inspect{idx}"
        inp = getattr(sample, "input", "")
        target = getattr(sample, "target", "")
        meta = getattr(sample, "metadata", {}) or {}
    return Task(task_id=str(sid), kind="inspect",
                payload={"input": inp, "metadata": meta},
                truth={"target": target})


class InspectTaskSource:
    def __init__(self, spec: dict, seed: int) -> None:
        self._spec = spec
        self._seed = seed
        self._limit = spec.get("limit")

    def _load_samples(self) -> list:
        # 路径①:直接给数据集文件(无需 inspect_ai)
        if self._spec.get("samples"):
            p = Path(self._spec["samples"])
            return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
        # 路径②:给一个 Inspect Task 引用(mod:attr),取其 dataset
        ref = self._spec.get("task")
        if not ref:
            raise LayerError("L14", "inspect",
                             "inspect 任务源需 spec.task(mod:attr)或 spec.samples(文件)")
        try:
            import importlib

            mod_name, _, attr = ref.partition(":")
            task_obj = getattr(importlib.import_module(mod_name), attr)
            task_obj = task_obj() if callable(task_obj) else task_obj
            dataset = getattr(task_obj, "dataset", task_obj)
            return list(dataset)
        except ImportError as exc:
            raise LayerError("L14", "inspect",
                             "缺 inspect_ai 依赖:uv sync --extra inspect") from exc
        except Exception as exc:
            raise LayerError("L14", "inspect", f"加载 Inspect Task 失败({ref}): {exc}") from exc

    def stream(self) -> list[Task]:
        samples = self._load_samples()
        rng = random.Random(self._seed)      # 同 seed → 序列逐位一致
        rng.shuffle(samples)
        if self._limit is not None:
            samples = samples[: self._limit]
        return [_sample_to_task(i, s) for i, s in enumerate(samples)]
