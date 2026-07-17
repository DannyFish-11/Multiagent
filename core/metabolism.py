"""代谢循环(M8):在自己的真实使用日志上做受控参数实验。

组成:
- RetrievalLogger:埋点。记录每次检索的 query、命中、是否被采用、用户反馈,
  追加写入 logs/retrieval_events.jsonl。
- run_metabolism():离线任务,手动触发,不自动运行。在日志回放集上做网格实验
  (检索 k / Matryoshka 维度 / 整合 prompt 变体 / 遗忘阈值),产出实验报告 +
  建议的 config diff(纯文本)。

严格边界(负向测试覆盖):
- 只产出建议,不写任何代码文件、不改 config.yaml —— 本模块没有指向它们的写路径;
  报告只落在 report_dir 下的新文件。
- config diff 必须人工审阅后手动应用(改进建议 → 人批准 → 生效,不闭环)。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.schemas import MultimodalInput

if TYPE_CHECKING:
    from adapters.memory import MemoryStore

# 网格实验的默认搜索空间
K_GRID = (1, 3, 5, 10)
MATRYOSHKA_GRID = (None,)          # 维度重嵌入代价高,默认只评当前维;可传入覆盖
FORGET_THRESHOLD_GRID = (0.0,)     # 预留位:遗忘阈值实验需可复算的时间衰减,同上
CONSOLIDATION_PROMPT_VARIANTS = ("v1-default",)  # 预留位:prompt 变体离线评估


@dataclass
class RetrievalEvent:
    query: str
    hit_ids: list[str]
    adopted_ids: list[str] = field(default_factory=list)  # 最终被 LLM 采用/用户认可的记忆
    feedback: str | None = None                            # "up" | "down" | None
    event_id: str = ""
    ts: float = field(default_factory=time.time)


class RetrievalLogger:
    def __init__(self, events_path: str | Path) -> None:
        self._path = Path(events_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: RetrievalEvent) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")

    def set_feedback(self, event_id: str, feedback: str, adopted_ids: list[str] | None = None) -> bool:
        """按 event_id 追加一条反馈修正记录(append-only,回放时后者覆盖前者)。"""
        if not self._path.exists():
            return False
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "event_id": event_id, "feedback": feedback,
                "adopted_ids": adopted_ids or [], "ts": time.time(),
                "kind": "feedback",
            }, ensure_ascii=False) + "\n")
        return True

    def load_events(self) -> list[RetrievalEvent]:
        if not self._path.exists():
            return []
        raw: dict[str, RetrievalEvent] = {}
        order: list[str] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                # 崩溃残留的半行坏记录:跳过,不拒读全量(见 audit.read_all 同条注释)
                continue
            if d.get("kind") == "feedback":
                ev = raw.get(d.get("event_id", ""))
                if ev:
                    ev.feedback = d.get("feedback")
                    if d.get("adopted_ids"):
                        ev.adopted_ids = list(d["adopted_ids"])
                continue
            ev = RetrievalEvent(
                query=d["query"], hit_ids=list(d.get("hit_ids", [])),
                adopted_ids=list(d.get("adopted_ids", [])),
                feedback=d.get("feedback"), event_id=d.get("event_id", ""),
                ts=float(d.get("ts", 0)),
            )
            key = ev.event_id or str(len(order))
            if key not in raw:
                order.append(key)
            raw[key] = ev
        return [raw[k] for k in order]


async def replay_hit_rate(memory: "MemoryStore", events: list[RetrievalEvent], k: int) -> float:
    """回放:对每个事件重跑检索(top-k),命中 = adopted_ids 至少一条进入 top-k。

    只统计有 ground truth(adopted_ids 非空且反馈不为 down)的事件。
    """
    scored = 0
    hit = 0
    for ev in events:
        if not ev.adopted_ids or ev.feedback == "down":
            continue
        scored += 1
        results = await memory.search(MultimodalInput.text(ev.query), k=k)
        got = {h.id for h in results}
        if got & set(ev.adopted_ids):
            hit += 1
    return hit / scored if scored else 0.0


async def run_metabolism(memory: "MemoryStore", events_path: str | Path,
                         report_dir: str | Path, current_k: int,
                         k_grid: tuple[int, ...] = K_GRID) -> dict[str, Any]:
    """离线代谢实验(手动触发)。返回报告 dict,并写入 report_dir 下的新报告文件。

    产出:各参数组的回放命中率 + 建议 config diff(文本)。不应用任何变更。
    """
    logger = RetrievalLogger(events_path)
    events = logger.load_events()
    usable = [e for e in events if e.adopted_ids and e.feedback != "down"]

    if not usable:
        # 零证据不给建议:样本为空时任何参数排序都是噪音
        report: dict[str, Any] = {
            "generated_at": time.time(),
            "events_total": len(events), "events_usable": 0,
            "grid_results": {}, "current_k": current_k, "recommended_k": current_k,
            "config_diff": "# 无可用回放事件(需 adopted_ids 且反馈非 down),不给出建议",
            "note": "样本不足;先积累带反馈的检索事件再运行代谢实验",
        }
        out_dir = Path(report_dir); out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"metabolism-{int(time.time())}.json"
        out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_path"] = str(out_file)
        return report

    results: dict[str, float] = {}
    for k in k_grid:
        results[f"k={k}"] = await replay_hit_rate(memory, events, k)

    best_k = max(k_grid, key=lambda k: (results[f"k={k}"], -k))  # 命中率同分取更小 k(更省)
    suggestion_lines = []
    if best_k != current_k:
        suggestion_lines.append("# 建议的 config.yaml diff(须人工审阅后手动应用):")
        suggestion_lines.append("agent:")
        suggestion_lines.append(f"-  top_k: {current_k}")
        suggestion_lines.append(f"+  top_k: {best_k}")
    else:
        suggestion_lines.append(f"# 当前 top_k={current_k} 已是回放集最优,无建议变更")

    report: dict[str, Any] = {
        "generated_at": time.time(),
        "events_total": len(events),
        "events_usable": len(usable),
        "grid_results": results,
        "current_k": current_k,
        "recommended_k": best_k,
        "config_diff": "\n".join(suggestion_lines),
        "note": "本报告仅为建议;应用与否由人类决定(M8 严格边界:不自改代码/配置)",
    }

    out_dir = Path(report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"metabolism-{int(time.time())}.json"
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(out_file)
    return report


def main() -> int:  # pragma: no cover - 组装层
    import argparse
    import asyncio

    from core.factory import get_config, get_shared_memory_store

    parser = argparse.ArgumentParser(prog="metabolism", description="离线代谢实验(手动触发)")
    parser.add_argument("--logs", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    cfg = get_config()
    memory = get_shared_memory_store(cfg)
    report = asyncio.run(run_metabolism(
        memory,
        args.logs or cfg.metabolism.events_path,
        args.out or cfg.metabolism.report_dir,
        current_k=cfg.agent.top_k,
    ))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
