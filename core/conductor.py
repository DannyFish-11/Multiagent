"""实验队列 conductor(PHASE5 M17 + M18)——薄编排,不自建平台。

接收实验 YAML → 入队 → 按并发上限派发 VM → 跟踪状态 → 收数据包 → 自验 → 通报。
状态机:queued → provisioning → running → verifying → done | failed | invalid | killed
全状态落盘,conductor 重启不丢队列。层二全局熔断 + 收件箱通报。

密钥纪律:VM 只拿该实验最小密钥集(实验专用受限 key),用后随 VM 销毁;主密钥不上云。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from core.breaker import GlobalBreaker
from core.sanity import run_sanity_checks

STATES = ("queued", "provisioning", "running", "verifying",
          "done", "failed", "invalid", "killed", "paused")


@dataclass
class ExperimentJob:
    experiment_id: str
    yaml_text: str
    budget_usd: float
    sanity_checks: list[str] = field(default_factory=list)
    state: str = "queued"
    vm_id: str = ""
    datapackage_url: str = ""
    submitted_at: float = 0.0
    updated_at: float = 0.0
    invalid_reasons: list[str] = field(default_factory=list)
    note: str = ""


class Conductor:
    def __init__(self, provider, breaker: GlobalBreaker, state_path: str | Path,
                 max_concurrent_vms: int = 2, notifier=None, image: str = "memory-agent:latest",
                 upload_base: str = "") -> None:
        self._provider = provider
        self._breaker = breaker
        self._path = Path(state_path)
        self._max_vms = max_concurrent_vms
        self._notifier = notifier
        self._image = image
        self._upload_base = upload_base
        self._jobs: dict[str, ExperimentJob] = {}
        self._load()

    # ---- 持久化(重启不丢队列) ----

    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._jobs = {k: ExperimentJob(**v) for k, v in data.items()}
            except (json.JSONDecodeError, OSError, TypeError):
                self._jobs = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({k: asdict(v) for k, v in self._jobs.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8")

    def _set_state(self, job: ExperimentJob, state: str, now: float) -> None:
        assert state in STATES, state
        job.state = state
        job.updated_at = now
        self._save()

    # ---- 队列 ----

    def submit(self, experiment_id: str, yaml_text: str, budget_usd: float,
               sanity_checks: list[str] | None = None, now: float | None = None) -> ExperimentJob:
        now = now if now is not None else 0.0
        job = ExperimentJob(experiment_id=experiment_id, yaml_text=yaml_text,
                            budget_usd=budget_usd, sanity_checks=sanity_checks or [],
                            submitted_at=now, updated_at=now)
        self._jobs[experiment_id] = job
        self._save()
        return job

    def _running_count(self) -> int:
        return sum(1 for j in self._jobs.values()
                   if j.state in ("provisioning", "running", "verifying"))

    def _inflight_committed(self) -> float:
        """在飞实验的承诺预算之和(已派发未收数据包);用于并发预留,防日额度透支。"""
        return sum(j.budget_usd for j in self._jobs.values()
                   if j.state in ("provisioning", "running", "verifying"))

    def queue_view(self) -> list[dict]:
        return [asdict(j) for j in self._jobs.values()]

    async def dispatch_ready(self, now: float, secrets_for=None) -> list[str]:
        """派发排队中的实验,受并发上限 + 层二全局熔断约束。返回本轮派发的 experiment_id。"""
        from adapters.cloud import build_cloud_init

        dispatched = []
        for job in list(self._jobs.values()):
            if job.state != "queued":
                continue
            if self._running_count() >= self._max_vms:
                break
            hit, why = self._breaker.global_cap_hit(now)
            if hit:
                await self._notify(f"[熔断] 停止派发 {job.experiment_id}:{why}")
                break  # 层二全局触顶:停止派发新 VM(halt-all)
            over, why_s = self._breaker.exceeds_structural_cap(job.budget_usd)
            if over:
                # 结构性超限(预算 > 日额度 × N%):任何余额下都不可派发 → 置终态,不堵塞队列
                job.invalid_reasons = [why_s]
                self._set_state(job, "invalid", now)
                await self._notify(f"[熔断] 拒绝 {job.experiment_id}(预算超限):{why_s}")
                continue
            # 并发预留:已实现花费 + 在飞承诺 + 本实验预算 不得超日额度(防并发透支)
            committed = self._inflight_committed()
            if (self._breaker.day_total(now) + committed + job.budget_usd
                    > self._breaker._cfg.global_daily_usd + 1e-9):  # noqa: SLF001
                await self._notify(f"[熔断] 暂缓 {job.experiment_id}:在飞实验预算已占满日额度")
                continue  # 该实验放不下,保持 queued;待在飞实验收单后重试
            within, why2 = self._breaker.experiment_within_ratio(job.budget_usd, now)
            if not within:
                # 瞬时占比超限(按当前日余额):保持 queued,下轮额度回收后重试
                await self._notify(f"[熔断] 暂缓 {job.experiment_id}:{why2}")
                continue
            secrets = secrets_for(job) if secrets_for else {}
            cloud_init = build_cloud_init(
                job.yaml_text, self._image,
                f"{self._upload_base}/{job.experiment_id}/datapackage.tar.zst")
            self._set_state(job, "provisioning", now)
            job.vm_id = await self._provider.create_vm(job.experiment_id, cloud_init, secrets)
            self._set_state(job, "running", now)
            dispatched.append(job.experiment_id)
        return dispatched

    async def collect(self, experiment_id: str, datapackage: dict, now: float,
                      datapackage_url: str = "") -> ExperimentJob:
        """VM 上传数据包后调用:自验 → 出结论或标 invalid → 销毁 VM → 通报。"""
        job = self._jobs[experiment_id]
        job.datapackage_url = datapackage_url
        self._set_state(job, "verifying", now)

        passed, results = run_sanity_checks(datapackage, job.sanity_checks)
        # 记全局熔断账
        spent = datapackage.get("metadata", {}).get("experiment_usd", 0.0)
        self._breaker.record(experiment_id, spent, now)

        if not passed:
            job.invalid_reasons = [r.detail for r in results if not r.passed]
            self._set_state(job, "invalid", now)
        else:
            self._set_state(job, "done", now)

        # 销毁 VM(确认自毁)
        if job.vm_id:
            await self._provider.destroy_vm(job.vm_id)

        await self._notify(self._compose_inbox(job, datapackage, results))
        return job

    async def kill(self, experiment_id: str, now: float, reason: str = "") -> None:
        job = self._jobs.get(experiment_id)
        if job is None:
            return
        if job.vm_id:
            await self._provider.destroy_vm(job.vm_id)
        job.note = reason
        self._set_state(job, "killed", now)
        await self._notify(f"[实验终止] {experiment_id}:{reason}")

    # ---- 收件箱(M18.3) ----

    def _compose_inbox(self, job: ExperimentJob, pkg: dict, results) -> str:
        flag = ""
        if job.state == "invalid":
            flag = " ⚠️INVALID"
        meta = pkg.get("metadata", {})
        failed = [r.name for r in results if not r.passed]
        lines = [
            f"[实验完成{flag}] {job.experiment_id}",
            f"状态:{job.state} | 任务:{meta.get('tasks_completed', 0)} | 花费:${meta.get('experiment_usd', 0):.4f}",
        ]
        if failed:
            lines.append(f"未通过的健全性检查:{failed}")
        if job.datapackage_url:
            lines.append(f"数据包:{job.datapackage_url}")
        if job.state == "done":
            lines.append(f"一句话结论:{pkg.get('one_line', '(见数据包)')}"
                         "(此为自动摘要,以数据包为准)")
        return "\n".join(lines)

    def daily_digest(self, now: float) -> dict:
        by_state: dict[str, int] = {}
        spend_by_exp: dict[str, float] = {}
        pending_decisions = []
        for j in self._jobs.values():
            by_state[j.state] = by_state.get(j.state, 0) + 1
            if j.state == "paused":
                pending_decisions.append({"experiment_id": j.experiment_id, "reason": j.note})
        for s in self._breaker._spend:  # noqa: SLF001 - 汇总用
            if s["ts"] >= now - 86400:
                spend_by_exp[s["experiment_id"]] = spend_by_exp.get(s["experiment_id"], 0.0) + s["usd"]
        return {"queue_by_state": by_state, "yesterday_spend": spend_by_exp,
                "pending_decisions": pending_decisions,
                "global_day_total": self._breaker.day_total(now)}

    async def _notify(self, message: str) -> None:
        if self._notifier is not None:
            await self._notifier.notify(message)
