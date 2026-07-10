"""PHASE5 M18 验收:自验 + 双层熔断 + 收件箱。"""

from __future__ import annotations

from core.breaker import BreakerConfig, GlobalBreaker
from core.conductor import Conductor
from core.sanity import run_sanity_checks
from adapters.cloud import LocalProcessProvider


class RecordingNotifier:
    def __init__(self):
        self.messages = []

    async def notify(self, message):
        self.messages.append(message)


def make_conductor(tmp_path, notifier, daily=100.0, ratio=0.5, max_vms=2):
    provider = LocalProcessProvider()
    breaker = GlobalBreaker(
        BreakerConfig(global_daily_usd=daily, single_experiment_max_ratio=ratio),
        tmp_path / "breaker.json")
    return Conductor(provider, breaker, tmp_path / "state.json",
                     max_concurrent_vms=max_vms, notifier=notifier), provider, breaker


# ---------------------------------------------------------------- 18.1 自验

def test_sanity_builtin_pass():
    pkg = {"metadata": {"tasks_completed": 50, "experiment_usd": 0.5, "budget_usd": 2.0},
           "audit": [{"ts": 1}, {"ts": 2}]}
    passed, results = run_sanity_checks(pkg, [])
    assert passed, [r.detail for r in results if not r.passed]


def test_sanity_zero_tasks_invalid():
    pkg = {"metadata": {"tasks_completed": 0, "experiment_usd": 0.5},
           "audit": [{"ts": 1}]}
    passed, _ = run_sanity_checks(pkg, [])
    assert not passed


def test_sanity_zero_cost_suspicious():
    pkg = {"metadata": {"tasks_completed": 10, "experiment_usd": 0.0},
           "audit": [{"ts": 1}]}
    passed, results = run_sanity_checks(pkg, [])
    assert not passed
    assert any("消耗为 0" in r.detail for r in results)
    # 显式声明离线零成本则放行
    pkg["allow_zero_cost"] = True
    passed2, _ = run_sanity_checks(pkg, [])
    assert passed2


def test_sanity_audit_gap_invalid():
    pkg = {"metadata": {"tasks_completed": 5, "experiment_usd": 0.5},
           "audit": [{"ts": 3}, {"ts": 1}]}  # 时间回退
    passed, _ = run_sanity_checks(pkg, [])
    assert not passed


def test_m15_injection_check():
    pkg = {"metadata": {"tasks_completed": 10, "experiment_usd": 0.5}, "audit": [{"ts": 1}],
           "arms": {"grader": {"bad_intercept_rate": {"injection": 0.0}},
                    "vote": {"bad_intercept_rate": {"injection": 0.0}}}}
    passed, results = run_sanity_checks(pkg, ["m15_injection_intercepted"])
    assert not passed  # 所有臂零拦截 → 判管道故障
    pkg["arms"]["grader"]["bad_intercept_rate"]["injection"] = 1.0
    passed2, _ = run_sanity_checks(pkg, ["m15_injection_intercepted"])
    assert passed2


async def test_invalid_experiment_not_reported_as_conclusion(tmp_path):
    """负向:管道坏掉的实验被标 invalid,数据保留但不产结论。"""
    notifier = RecordingNotifier()
    cond, provider, _ = make_conductor(tmp_path, notifier)
    cond.submit("bad_exp", "yaml", budget_usd=2.0, now=0.0)
    await cond.dispatch_ready(now=1.0)
    # 零任务数据包 → sanity 失败
    bad_pkg = {"metadata": {"tasks_completed": 0, "experiment_usd": 0.0}, "audit": []}
    job = await cond.collect("bad_exp", bad_pkg, now=2.0)
    assert job.state == "invalid"
    assert job.invalid_reasons  # 数据保留 + 原因记录
    # 通报含 INVALID 标记,不含"一句话结论"
    msg = notifier.messages[-1]
    assert "INVALID" in msg and "一句话结论" not in msg


# ---------------------------------------------------------------- 18.2 双层熔断

def test_global_daily_cap_blocks_dispatch():
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    breaker = GlobalBreaker(BreakerConfig(global_daily_usd=10.0), tmp / "b.json")
    breaker.record("exp1", 10.0, now=100.0)  # 灌满日额度
    ok, reason = breaker.can_dispatch(1.0, now=100.0)
    assert not ok and "日额度触顶" in reason


def test_single_experiment_ratio_cap():
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    breaker = GlobalBreaker(BreakerConfig(global_daily_usd=100.0, single_experiment_max_ratio=0.5),
                            tmp / "b.json")
    # 单实验预算 60 > 全局日额度 100 的 50%
    ok, reason = breaker.can_dispatch(60.0, now=0.0)
    assert not ok and "50%" in reason
    # 40 < 50 放行
    assert breaker.can_dispatch(40.0, now=0.0)[0]


async def test_breaker_stops_dispatch_and_notifies(tmp_path):
    notifier = RecordingNotifier()
    cond, _, breaker = make_conductor(tmp_path, notifier, daily=10.0)
    breaker.record("prev", 10.0, now=0.0)  # 已烧穿日额度
    cond.submit("exp1", "yaml", budget_usd=2.0, now=0.0)
    dispatched = await cond.dispatch_ready(now=1.0)
    assert dispatched == []  # 熔断:不派发
    assert any("熔断" in m for m in notifier.messages)


def test_single_experiment_ratio_against_remaining_balance():
    """占比闸门须按**日余额**判定:已花掉大部分额度后,大实验即便未超日额度 N% 也应被拒。"""
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    breaker = GlobalBreaker(BreakerConfig(global_daily_usd=100.0, single_experiment_max_ratio=0.5),
                            tmp / "b.json")
    breaker.record("prev", 80.0, now=0.0)  # 已花 80,余额仅 20
    # 预算 40:未超日额度的 50%(=50),但超过日余额 20 的 50%(=10)→ 应拒
    ok, reason = breaker.can_dispatch(40.0, now=0.0)
    assert not ok and "余额" in reason, reason
    # 预算 10:恰为余额 20 的 50% → 放行(边界含)
    assert breaker.can_dispatch(10.0, now=0.0)[0]


async def test_oversized_head_does_not_starve_smaller_job(tmp_path):
    """队列头一个结构性超限的大实验不得永久堵塞后面的小实验(超限=置终态,非停派)。"""
    notifier = RecordingNotifier()
    cond, _, _ = make_conductor(tmp_path, notifier, daily=100.0, ratio=0.5, max_vms=2)
    cond.submit("big", "yaml", budget_usd=60.0, now=0.0)    # 60 > 50%*100 → 结构性拒绝
    cond.submit("small", "yaml", budget_usd=5.0, now=0.0)   # 应被派发
    dispatched = await cond.dispatch_ready(now=1.0)
    assert dispatched == ["small"], dispatched
    assert cond._jobs["big"].state == "invalid"       # 大实验终态拒绝(带原因),不再堵塞
    assert cond._jobs["big"].invalid_reasons
    assert cond._jobs["small"].state == "running"
    assert any("拒绝 big" in m for m in notifier.messages)


async def test_inflight_budget_reservation_prevents_daily_overshoot(tmp_path):
    """并发预留:两个各 50% 的实验在飞后,第三个即便自身合规也不得再派(否则透支日额度)。"""
    notifier = RecordingNotifier()
    # daily=100, ratio=0.5 → 单实验上限 50;max_vms=3 让并发不先于预留触顶
    cond, _, _ = make_conductor(tmp_path, notifier, daily=100.0, ratio=0.5, max_vms=3)
    cond.submit("a", "yaml", budget_usd=50.0, now=0.0)
    cond.submit("b", "yaml", budget_usd=50.0, now=0.0)
    cond.submit("c", "yaml", budget_usd=1.0, now=0.0)   # 合规但日额度已被 a+b 预留占满
    dispatched = await cond.dispatch_ready(now=1.0)
    assert dispatched == ["a", "b"], dispatched
    assert cond._jobs["c"].state == "queued"   # c 保持排队,待 a/b 收单回收额度后重试
    assert any("在飞实验预算已占满" in m for m in notifier.messages)


# ---------------------------------------------------------------- 18.3 收件箱

async def test_done_notification_has_summary(tmp_path):
    notifier = RecordingNotifier()
    cond, _, _ = make_conductor(tmp_path, notifier)
    cond.submit("exp1", "yaml", budget_usd=2.0, now=0.0)
    await cond.dispatch_ready(now=1.0)
    pkg = {"metadata": {"tasks_completed": 50, "experiment_usd": 0.5}, "audit": [{"ts": 1}],
           "one_line": "grader 与 vote 无显著差异"}
    await cond.collect("exp1", pkg, now=2.0, datapackage_url="http://store/dp")
    msg = notifier.messages[-1]
    assert "一句话结论" in msg and "以数据包为准" in msg
    assert "http://store/dp" in msg


def test_daily_digest(tmp_path):
    notifier = RecordingNotifier()
    cond, _, breaker = make_conductor(tmp_path, notifier)
    breaker.record("exp1", 3.0, now=100.0)
    breaker.record("exp2", 1.5, now=100.0)
    cond.submit("exp3", "yaml", budget_usd=2.0, now=100.0)
    digest = cond.daily_digest(now=101.0)
    assert digest["queue_by_state"].get("queued") == 1
    assert digest["yesterday_spend"]["exp1"] == 3.0
    assert digest["global_day_total"] == 4.5
