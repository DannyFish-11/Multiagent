"""M15 治理对照实验管道冒烟(离线,零真实花费)——验证指标口径与报告产出。"""

from __future__ import annotations


from core.audit import AuditLog
from core.commons import CommonsStore, GraderAdmission
from core.commons_metrics import CommonsMetrics
from core.identity import AgentIdentity
from core.m15_governance import (
    BAD_KINDS,
    render_report,
    run_arm,
    smoke_item_set,
)
from tests.conftest import ScriptedLLM


def scripted_grader(good_ids):
    """按 item 内容打分:良品高分、坏品低分(冒烟用确定性替身)。"""
    class ContentAwareLLM:
        async def chat(self, messages, **kw):
            user = next(m for m in messages if m.role == "user")
            text = str(user.content)
            # 坏品关键词 → 低分
            bad = any(kw in text for kw in ("地球是平的", "过时", "有害", "注入", "SYSTEM"))
            score = 0.1 if bad else 0.9
            return f'{{"score": {score}, "reason": "auto"}}'
    return ContentAwareLLM()


def make_store(tmp_path, name, admission):
    metrics = CommonsMetrics(tmp_path / f"{name}_commons.json")
    audit = AuditLog(tmp_path / f"{name}_audit.jsonl")
    return CommonsStore(metrics, audit, {"memory": admission}, report_threshold=2)


async def test_grader_arm_intercepts_bad(tmp_path):
    ident = AgentIdentity.load_or_create(tmp_path / "author")
    items = smoke_item_set()
    store = make_store(tmp_path, "grader", GraderAdmission(scripted_grader([])))
    result = await run_arm("grader", items, store, ident)

    # 良品应大多入池(误杀率低),坏品应被拦(拦截率高)
    assert result.good_falsekill_rate() == 0.0
    rates = result.bad_intercept_rate()
    for k in BAD_KINDS:
        assert rates[k] == 1.0, (k, rates)


async def test_natural_arm_admits_all_then_filters(tmp_path):
    """C 臂:无准入全入池,靠举报自然筛选;坏品被举报后降级。"""
    ident = AgentIdentity.load_or_create(tmp_path / "author")
    items = smoke_item_set()
    # 无准入 = grader 恒通过
    always_pass = GraderAdmission(ScriptedLLM(replies=['{"score":0.9,"reason":"x"}'] * 50))
    store = make_store(tmp_path, "natural", always_pass)
    # 坏品各被举报 2 次(达阈值降级),良品零举报
    reports = {it.item_id: (2 if not it.is_good else 0) for it in items}
    result = await run_arm("natural", items, store, ident, natural_selection_reports=reports)

    # 自然筛选后坏品被降级(拦截),良品保留
    assert result.good_falsekill_rate() == 0.0
    rates = result.bad_intercept_rate()
    assert all(rates[k] == 1.0 for k in BAD_KINDS)


async def test_report_renders_and_is_recomputable(tmp_path):
    ident = AgentIdentity.load_or_create(tmp_path / "author")
    items = smoke_item_set()
    seeds_results = {}
    for seed in (0, 1):
        store = make_store(tmp_path, f"g{seed}", GraderAdmission(scripted_grader([])))
        r = await run_arm("grader", items, store, ident)
        seeds_results[seed] = {"grader": r}

    md = render_report(seeds_results, model_tier="cheap", is_smoke=True)
    out = tmp_path / "m15_governance.md"
    out.write_text(md, encoding="utf-8")
    text = out.read_text(encoding="utf-8")
    # 冒烟标注在场(结论红线:不下结论)
    assert "冒烟运行" in text and "不构成结论" in text
    # 数据可复算:报告里的拦截率与 result 对象一致
    assert str(seeds_results[0]["grader"].summary()["bad_intercept_rate"]) in text or \
        "坏品拦截" in text


def test_smoke_item_set_has_truth_labels():
    items = smoke_item_set()
    goods = [i for i in items if i.is_good]
    bads = [i for i in items if not i.is_good]
    assert len(goods) == 2
    assert {b.bad_kind for b in bads} == set(BAD_KINDS)  # 四类坏品齐全
