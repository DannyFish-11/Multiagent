"""实验自带健全性检查(PHASE5 M18.1)——治验证债。

每份实验结束后、出报告前自动执行 sanity_checks;任一失败则整个实验标记 invalid
(数据保留,结论不生成)。内置通用检查 + 作者声明的实验特定检查。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


# 通用检查签名:(datapackage: dict) -> CheckResult
CheckFn = Callable[[dict], CheckResult]


def check_tasks_completed(pkg: dict) -> CheckResult:
    n = pkg.get("metadata", {}).get("tasks_completed", 0)
    return CheckResult("tasks_completed>0", n > 0, f"完成 {n} 个任务")


def check_plan_matches_actual(pkg: dict) -> CheckResult:
    """各臂实际执行的任务序列与计划一致。"""
    planned = pkg.get("planned_task_ids")
    actual = pkg.get("actual_task_ids")
    if planned is None or actual is None:
        return CheckResult("plan_matches_actual", True, "未声明计划序列,跳过")
    ok = list(planned) == list(actual)
    return CheckResult("plan_matches_actual", ok,
                       "序列一致" if ok else f"计划 {len(planned)} 实际 {len(actual)} 不一致")


def check_audit_no_gaps(pkg: dict) -> CheckResult:
    """审计日志无缺口(时间戳单调、无回退)。"""
    ts = [e.get("ts", 0) for e in pkg.get("audit", [])]
    ok = ts == sorted(ts)
    return CheckResult("audit_no_gaps", ok, "审计时间戳单调" if ok else "审计日志时间回退,疑有缺口")


def check_budget_in_range(pkg: dict) -> CheckResult:
    """预算消耗在声明区间内(消耗为 0 同样可疑)。"""
    spent = pkg.get("metadata", {}).get("experiment_usd", 0.0)
    lo = pkg.get("budget_min", 0.0)
    hi = pkg.get("metadata", {}).get("budget_usd", pkg.get("budget_max", float("inf")))
    # 消耗为 0 可疑(除非显式声明离线零成本)
    if spent == 0 and not pkg.get("allow_zero_cost", False):
        return CheckResult("budget_in_range", False, "预算消耗为 0,疑管道未真正执行")
    ok = lo <= spent <= hi
    return CheckResult("budget_in_range", ok, f"消耗 ${spent:.4f}(区间 [{lo}, {hi}])")


BUILTIN_CHECKS: dict[str, CheckFn] = {
    "tasks_completed": check_tasks_completed,
    "plan_matches_actual": check_plan_matches_actual,
    "audit_no_gaps": check_audit_no_gaps,
    "budget_in_range": check_budget_in_range,
}


# 实验特定检查注册表(作者按 experiment 类型声明)
_CUSTOM: dict[str, CheckFn] = {}


def register_check(name: str, fn: CheckFn) -> None:
    _CUSTOM[name] = fn


def m15_injection_intercepted(pkg: dict) -> CheckResult:
    """M15 特定:注入坏品若所有臂零拦截,判管道故障而非结论。"""
    arms = pkg.get("arms", {})
    if not arms:
        return CheckResult("m15_injection_intercepted", True, "无臂数据,跳过")
    any_intercept = any(
        a.get("bad_intercept_rate", {}).get("injection", 0) > 0 for a in arms.values())
    return CheckResult("m15_injection_intercepted", any_intercept,
                       "至少一臂拦截注入" if any_intercept else "所有臂零拦截注入 → 管道故障")


def m16_control_rotation_uniform(pkg: dict) -> CheckResult:
    """M16 特定:对照臂轮转分配确实均匀(各实例任务数极差 ≤ 1)。"""
    dist = pkg.get("control_task_distribution", {})
    if not dist:
        return CheckResult("m16_control_rotation_uniform", True, "无对照分布,跳过")
    vals = list(dist.values())
    ok = max(vals) - min(vals) <= 1
    return CheckResult("m16_control_rotation_uniform", ok,
                       f"极差 {max(vals) - min(vals)}")


register_check("m15_injection_intercepted", m15_injection_intercepted)
register_check("m16_control_rotation_uniform", m16_control_rotation_uniform)


def run_sanity_checks(pkg: dict, declared: list[str]) -> tuple[bool, list[CheckResult]]:
    """执行内置 + 声明的检查。返回 (全部通过?, 结果列表)。"""
    results: list[CheckResult] = []
    # 内置全跑
    for fn in BUILTIN_CHECKS.values():
        results.append(fn(pkg))
    # 声明的实验特定检查
    for name in declared:
        fn = _CUSTOM.get(name) or BUILTIN_CHECKS.get(name)
        if fn is None:
            results.append(CheckResult(name, False, "未注册的检查名"))
        elif name not in BUILTIN_CHECKS:
            results.append(fn(pkg))
    all_passed = all(r.passed for r in results)
    return all_passed, results
