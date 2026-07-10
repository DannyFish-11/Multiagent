"""PHASE5 M17 验收:云端底座(队列 + 状态机 + 并发上限 + VM 生命周期)。"""

from __future__ import annotations

from core.breaker import BreakerConfig, GlobalBreaker
from core.conductor import Conductor
from adapters.cloud import LocalProcessProvider, build_cloud_init


def make_conductor(tmp_path, max_vms=2, notifier=None):
    provider = LocalProcessProvider()
    breaker = GlobalBreaker(BreakerConfig(global_daily_usd=100.0), tmp_path / "breaker.json")
    cond = Conductor(provider, breaker, tmp_path / "state.json",
                     max_concurrent_vms=max_vms, notifier=notifier, upload_base="http://store")
    return cond, provider


def datapackage(tasks=50, usd=0.5, arms=None, injection_ok=True):
    pkg = {
        "metadata": {"tasks_completed": tasks, "experiment_usd": usd, "budget_usd": 2.0},
        "audit": [{"ts": 1}, {"ts": 2}, {"ts": 3}],
        "one_line": "冒烟结论",
    }
    if arms is not None:
        pkg["arms"] = arms
    return pkg


# ---------------------------------------------------------------- 队列 + 并发

async def test_submit_and_dispatch_respects_concurrency(tmp_path):
    cond, provider = make_conductor(tmp_path, max_vms=2)
    for i in range(3):
        cond.submit(f"exp{i}", "yaml", budget_usd=2.0, now=float(i))
    dispatched = await cond.dispatch_ready(now=10.0)
    # 并发上限 2:只派 2 个,第 3 个仍排队
    assert len(dispatched) == 2
    states = {j["experiment_id"]: j["state"] for j in cond.queue_view()}
    assert sum(1 for s in states.values() if s == "running") == 2
    assert sum(1 for s in states.values() if s == "queued") == 1


async def test_full_lifecycle_to_done_and_vm_destroyed(tmp_path):
    cond, provider = make_conductor(tmp_path)
    cond.submit("exp1", "yaml", budget_usd=2.0, now=0.0)
    await cond.dispatch_ready(now=1.0)
    vm_id = cond.queue_view()[0]["vm_id"]
    assert await provider.get_status(vm_id) == "running"

    job = await cond.collect("exp1", datapackage(), now=2.0,
                             datapackage_url="http://store/exp1/dp.tar.zst")
    assert job.state == "done"
    # VM 确认销毁
    assert await provider.get_status(vm_id) == "terminated"
    assert job.datapackage_url.endswith("dp.tar.zst")


async def test_state_persists_across_restart(tmp_path):
    cond, _ = make_conductor(tmp_path)
    cond.submit("exp1", "yaml", budget_usd=2.0, now=0.0)
    await cond.dispatch_ready(now=1.0)
    # 新 conductor 从同一 state 文件重建(重启不丢队列)
    breaker = GlobalBreaker(BreakerConfig(), tmp_path / "breaker.json")
    cond2 = Conductor(LocalProcessProvider(), breaker, tmp_path / "state.json")
    view = cond2.queue_view()
    assert len(view) == 1 and view[0]["experiment_id"] == "exp1"
    assert view[0]["state"] == "running"


async def test_cloud_init_has_selfdestruct(tmp_path):
    ci = build_cloud_init("experiment: x", "memory-agent:latest", "http://store/dp")
    assert "docker run" in ci and "shutdown -h now" in ci
    assert "http://store/dp" in ci  # 上传后自毁


async def test_kill_terminates_vm(tmp_path):
    cond, provider = make_conductor(tmp_path)
    cond.submit("exp1", "yaml", budget_usd=2.0, now=0.0)
    await cond.dispatch_ready(now=1.0)
    vm_id = cond.queue_view()[0]["vm_id"]
    await cond.kill("exp1", now=2.0, reason="人工终止")
    assert cond.queue_view()[0]["state"] == "killed"
    assert await provider.get_status(vm_id) == "terminated"
