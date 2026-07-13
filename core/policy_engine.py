"""声明式分级规则引擎(PHASE3 M9.2)。

规则全部在 config.approval.policies 声明(动作类型 × 参数条件 → auto|confirm|deny),
不写死在代码。自上而下首条命中生效;无命中用 default_level。

谓词算子(参数键后缀):
  field            相等
  field__gte/lte   数值 >= / <=
  field__in/not_in 列表包含
  field__regex     正则搜索
  field__contains  子串
支持点路径取值(如 payee.domain)。action 用 fnmatch 通配。
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any

from core.config import PolicyRule


def _get(params: dict, path: str) -> Any:
    cur: Any = params
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _match_predicate(params: dict, key: str, expected: Any) -> bool:
    for op in ("__gte", "__lte", "__in", "__not_in", "__regex", "__contains"):
        if key.endswith(op):
            field = key[: -len(op)]
            actual = _get(params, field)
            # 类型不符(如数值谓词遇到字符串/None)按**不匹配**处理,绝不让 TypeError 冒出来
            # 中断整条策略评估(评估崩溃比谓词落空危险得多;落空后回落 default_level)。
            try:
                if op == "__gte":
                    return actual is not None and actual >= expected
                if op == "__lte":
                    return actual is not None and actual <= expected
                if op == "__in":
                    return actual in expected
                if op == "__not_in":
                    return actual not in expected
                if op == "__regex":
                    return actual is not None and re.search(expected, str(actual)) is not None
                if op == "__contains":
                    return actual is not None and expected in actual
            except TypeError:
                return False
    return _get(params, key) == expected


def evaluate(rules: list[PolicyRule], default_level: str,
             action: str, params: dict) -> tuple[str, str]:
    """返回 (level, reason)。首条 action 通配命中且全部谓词成立的规则生效。"""
    for rule in rules:
        if not fnmatch.fnmatch(action, rule.action):
            continue
        if all(_match_predicate(params, k, v) for k, v in rule.when.items()):
            return rule.level, rule.reason or f"命中规则 {rule.action}"
    return default_level, f"无规则命中,采用默认级别 {default_level}"
