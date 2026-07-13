"""M32 验收:Gecko-lite 预执行模拟(参数校验 + 效果预览)。

覆盖:规则层校验(required/type/enum);语义校验(LLM,可选);效果预览(工具自报确定性
优先、LLM 估计、schema 摘要兜底);接入工具循环(非法参数不执行、回灌纠错反馈);预览
经审批闸透传到待批项/审计;simulator=None 向后兼容。
"""

from __future__ import annotations

import asyncio

from core.approval import ApprovalQueue, Notifier
from core.audit import AuditLog
from core.config import ApprovalSettings, PolicyRule, SimulationSettings, load_config
from core.schemas import MemoryHit, MultimodalInput
from core.simulate import Simulator, validate_rules
from core.tool_agent import ToolAgent
from core.tools import AssistantTurn, Tool, ToolCall
from tests.conftest import ScriptedLLM


class FakeMemory:
    def __init__(self):
        self.added = []

    async def search(self, query: MultimodalInput, k: int = 5):
        return [MemoryHit(id="m1", score=0.9, content="记忆")]

    async def add(self, inp: MultimodalInput, meta=None):
        self.added.append(inp.content)
        return "id1"


class ScriptedToolLLM:
    def __init__(self, turns):
        self._turns = list(turns)
        self._i = 0

    async def chat_tools(self, messages, tools, **kw):
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        return turn


_SCHEMA = {"type": "object", "properties": {
    "ticker": {"type": "string"},
    "qty": {"type": "integer"},
    "side": {"type": "string", "enum": ["buy", "sell"]}},
    "required": ["ticker", "qty"]}


# ---------------------------------------------------------------- ① 规则层校验

def test_validate_rules_required_type_enum():
    ok, _ = validate_rules(_SCHEMA, {"ticker": "AAPL", "qty": 10, "side": "buy"})
    assert ok
    ok, reason = validate_rules(_SCHEMA, {"ticker": "AAPL"})          # 缺 required qty
    assert not ok and "qty" in reason
    ok, reason = validate_rules(_SCHEMA, {"ticker": "AAPL", "qty": "十"})  # 类型不符
    assert not ok and "类型" in reason
    ok, reason = validate_rules(_SCHEMA, {"ticker": "AAPL", "qty": 1, "side": "hold"})  # enum
    assert not ok and "side" in reason


def test_validate_rules_bool_is_not_integer():
    # bool 是 int 子类,但 qty=True 不是合法整数参数
    ok, reason = validate_rules(_SCHEMA, {"ticker": "AAPL", "qty": True})
    assert not ok and "qty" in reason


def test_validate_rules_unknown_field_not_blocked():
    # 未知字段不硬拦(低误报);仍视为通过
    ok, _ = validate_rules(_SCHEMA, {"ticker": "AAPL", "qty": 1, "extra": "x"})
    assert ok


# ---------------------------------------------------------------- ② assess:校验 + 预览

def _sim(**kw):
    return Simulator(SimulationSettings(**kw))


async def test_assess_blocks_invalid_args():
    tool = Tool("trade", "下单", _SCHEMA, lambda a: None, mutating=True)
    a = await _sim().assess(tool, {"ticker": "AAPL"})       # 缺 qty
    assert not a.ok and "qty" in a.reason and a.effect == ""


async def test_assess_uses_tool_preview_for_mutating():
    async def preview(args):
        return f"将下单 {args['side']} {args['qty']} 股 {args['ticker']}"
    tool = Tool("trade", "下单", _SCHEMA, lambda a: None, mutating=True, preview=preview)
    a = await _sim().assess(tool, {"ticker": "AAPL", "qty": 5, "side": "buy"})
    assert a.ok and a.effect == "将下单 buy 5 股 AAPL" and a.estimated is False


async def test_assess_generic_summary_when_no_preview():
    tool = Tool("trade", "下单", _SCHEMA, lambda a: None, mutating=True)
    a = await _sim().assess(tool, {"ticker": "AAPL", "qty": 5})
    assert a.ok and "trade" in a.effect and "AAPL" in a.effect and a.estimated is False


async def test_assess_safe_read_tool_no_preview():
    tool = Tool("recall", "检索", {"type": "object", "properties": {}}, lambda a: None, safe=True)
    a = await _sim().assess(tool, {})
    assert a.ok and a.effect == ""


async def test_assess_disabled_skips_validation():
    # enabled=False:不做规则校验(也不预览),始终放行
    tool = Tool("trade", "下单", _SCHEMA, lambda a: None, mutating=True)
    a = await _sim(enabled=False).assess(tool, {"ticker": "AAPL"})   # 缺 qty 也放行
    assert a.ok


# ---------------------------------------------------------------- ③ 语义校验 / LLM 预览(可选)

async def test_semantic_validation_rejects_wrong_meaning():
    llm = ScriptedLLM(replies=['{"ok": false, "reason": "ticker 应为代码如 AAPL,而非公司全名"}'])
    sim = Simulator(SimulationSettings(semantic_validation=True), llm=llm)
    tool = Tool("trade", "下单", _SCHEMA, lambda a: None, mutating=True)
    a = await sim.assess(tool, {"ticker": "Apple Inc.", "qty": 1})   # schema 合法但语义不对
    assert not a.ok and "AAPL" in a.reason


async def test_semantic_validation_fail_open_on_bad_json():
    # 校验器输出无法解析 → fail-open(放行),不误伤合法调用
    llm = ScriptedLLM(replies=["不是 JSON"])
    sim = Simulator(SimulationSettings(semantic_validation=True), llm=llm)
    tool = Tool("trade", "下单", _SCHEMA, lambda a: None, mutating=True)
    a = await sim.assess(tool, {"ticker": "AAPL", "qty": 1})
    assert a.ok


async def test_llm_preview_marks_estimated():
    llm = ScriptedLLM(replies=["将以市价买入 1 股苹果,不可撤销"])
    sim = Simulator(SimulationSettings(llm_preview=True), llm=llm)
    tool = Tool("trade", "下单", _SCHEMA, lambda a: None, mutating=True)   # 无自报 preview
    a = await sim.assess(tool, {"ticker": "AAPL", "qty": 1})
    assert a.ok and a.effect == "将以市价买入 1 股苹果,不可撤销" and a.estimated is True


# ---------------------------------------------------------------- ④ 接入工具循环

def _agent(llm, mem, tools, approval=None, simulator=None):
    return ToolAgent(llm, mem, load_config(), approval=approval, tools=tools,
                     simulator=simulator)


async def test_loop_invalid_args_feed_back_not_executed():
    """非法参数:不执行工具、把纠错反馈回灌 LLM,LLM 修正后再成功。"""
    mem = FakeMemory()
    ran = []

    async def _run(args):
        ran.append(args)
        return "ok"
    tool = Tool("trade", "下单", _SCHEMA, _run, mutating=True, safe=True)
    llm = ScriptedToolLLM([
        AssistantTurn(tool_calls=[ToolCall("c1", "trade", {"ticker": "AAPL"})]),   # 缺 qty
        AssistantTurn(tool_calls=[ToolCall("c2", "trade", {"ticker": "AAPL", "qty": 3})]),
        AssistantTurn(content="已下单。"),
    ])
    resp = await _agent(llm, mem, [tool], simulator=_sim()).run("买 3 股苹果")
    assert ran == [{"ticker": "AAPL", "qty": 3}]     # 第一次非法未执行,只有修正后的成功
    assert resp.reply == "已下单。"


async def test_loop_preview_reaches_approval(tmp_path):
    """效果预览随 confirm 动作透传到待批项与审计。"""
    mem = FakeMemory()

    async def preview(args):
        return f"将向 {args['payee']} 付款 ${args['amount']}"

    async def _pay(args):
        return "paid"
    tool = Tool("pay", "付款", {"type": "object", "properties": {
        "payee": {"type": "string"}, "amount": {"type": "number"}},
        "required": ["payee", "amount"]}, _pay,
        action="pay", mutating=True, preview=preview)

    s = ApprovalSettings(audit_path=str(tmp_path / "a.jsonl"), default_level="auto",
                         timeout_s=1.0,
                         policies=[PolicyRule(action="pay", when={}, level="confirm")])
    audit = AuditLog(s.audit_path)
    q = ApprovalQueue(s, audit, Notifier(s))
    llm = ScriptedToolLLM([
        AssistantTurn(tool_calls=[ToolCall("c1", "pay", {"payee": "shop", "amount": 9})]),
        AssistantTurn(content="done"),
    ])
    agent = _agent(llm, mem, [tool], approval=q, simulator=_sim())
    task = asyncio.create_task(agent.run("给 shop 付 9 块"))
    await asyncio.sleep(0.05)
    pend = q.list_pending()
    assert len(pend) == 1
    assert pend[0]["preview"] == "将向 shop 付款 $9"      # 批准人看得到后果
    await q.resolve(pend[0]["id"], approved=True)
    await task
    # 审计也留下预览(extra 字段在 record 内被合并到顶层)
    rec = [e for e in audit.read_all() if e["action"] == "pay"][-1]
    assert rec["preview"] == "将向 shop 付款 $9"


async def test_backward_compatible_without_simulator():
    """simulator=None:行为与接入前一致(不校验、不预览、直接执行)。"""
    mem = FakeMemory()
    ran = []

    async def _run(args):
        ran.append(args)
        return "ok"
    tool = Tool("trade", "下单", _SCHEMA, _run, mutating=True, safe=True)
    llm = ScriptedToolLLM([
        AssistantTurn(tool_calls=[ToolCall("c1", "trade", {"ticker": "AAPL"})]),  # 缺 qty
        AssistantTurn(content="done"),
    ])
    await _agent(llm, mem, [tool], simulator=None).run("下单")
    assert ran == [{"ticker": "AAPL"}]      # 无模拟:非法参数照样执行(旧行为)
