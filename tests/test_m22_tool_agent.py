"""M22 验收:会用工具的 agent 循环(离线,ScriptedToolLLM 驱动)。

覆盖:LLM 决策调用工具→执行→回灌→最终回答;审批 deny 被优雅回灌;循环硬上限
loop_capped;工具箱按 config 组装;services 按 autonomy=tools 装配 ToolAgent。
"""

from __future__ import annotations

from core.config import ApprovalSettings, PolicyRule, load_config
from core.schemas import MemoryHit, MultimodalInput
from core.tool_agent import ToolAgent
from core.tools import AssistantTurn, Tool, ToolCall, build_toolbox, recall_tool, remember_tool


class FakeMemory:
    def __init__(self):
        self.added = []

    async def search(self, query: MultimodalInput, k: int = 5):
        return [MemoryHit(id="m1", score=0.9, content="我的猫叫 Benjamin")]

    async def add(self, inp: MultimodalInput, meta=None):
        self.added.append(inp.content)
        return "id1"


class ScriptedToolLLM:
    """按脚本逐步返回 AssistantTurn(实现 chat_tools);越界后重复最后一步。"""
    def __init__(self, turns):
        self._turns = list(turns)
        self._i = 0
        self.calls_seen = []

    async def chat_tools(self, messages, tools, **kw):
        self.calls_seen.append((messages, tools, kw))
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        return turn


def _agent(llm, mem, tools, approval=None, **cfg_over):
    return ToolAgent(llm, mem, load_config(**cfg_over), approval=approval, tools=tools)


# ---------------------------------------------------------------- 工具循环

async def test_loop_calls_tool_then_answers():
    mem = FakeMemory()
    ran = []

    async def _recall(args):
        ran.append(args)
        return '[{"content":"我的猫叫 Benjamin"}]'
    tool = Tool("recall", "检索", {"type": "object", "properties": {}}, _recall, safe=True)

    llm = ScriptedToolLLM([
        AssistantTurn(tool_calls=[ToolCall("c1", "recall", {"query": "猫"})]),
        AssistantTurn(content="你的猫叫 Benjamin。"),
    ])
    resp = await _agent(llm, mem, [tool]).run("我的猫叫什么?")
    assert ran == [{"query": "猫"}]                       # 工具真被调用
    assert resp.reply == "你的猫叫 Benjamin。"             # 拿到结果后给最终答案
    assert mem.added == ["我的猫叫什么?"]                 # 交互写入记忆


async def test_remember_tool_writes_memory():
    mem = FakeMemory()
    llm = ScriptedToolLLM([
        AssistantTurn(tool_calls=[ToolCall("c1", "remember", {"text": "用户喜欢蓝色"})]),
        AssistantTurn(content="记住了。"),
    ])
    await _agent(llm, mem, [remember_tool(mem)]).run("记住我喜欢蓝色")
    assert "用户喜欢蓝色" in mem.added                     # remember 工具写入


# ---------------------------------------------------------------- 审批治理

async def test_denied_tool_is_gated_and_fed_back(tmp_path):
    from core.approval import ApprovalQueue, Notifier
    from core.audit import AuditLog

    settings = ApprovalSettings(
        policies=[PolicyRule(action="danger", when={}, level="deny")], default_level="auto")
    approval = ApprovalQueue(settings, AuditLog(tmp_path / "a.jsonl"), Notifier(settings))

    mem = FakeMemory()
    ran = []

    async def _danger(args):
        ran.append(args)                                  # 不应被执行(被 deny 拦在闸前)
        return "done"
    danger = Tool("danger", "危险动作", {"type": "object", "properties": {}}, _danger)

    llm = ScriptedToolLLM([
        AssistantTurn(tool_calls=[ToolCall("c1", "danger", {})]),
        AssistantTurn(content="那个动作我没法执行。"),
    ])
    resp = await _agent(llm, mem, [danger], approval=approval).run("干点危险的")
    assert ran == []                                      # deny → execute 从未运行
    assert resp.reply == "那个动作我没法执行。"            # 拒绝被回灌,LLM 优雅收尾


async def test_safe_tool_auto_passes_under_confirm_default(tmp_path):
    """默认 default_level=confirm 也不会卡住 recall/remember(safe → level_override=auto)。"""
    from core.approval import ApprovalQueue, Notifier
    from core.audit import AuditLog

    settings = ApprovalSettings(default_level="confirm")   # 无规则 → 本应 confirm(会阻塞)
    approval = ApprovalQueue(settings, AuditLog(tmp_path / "a.jsonl"), Notifier(settings))
    mem = FakeMemory()
    llm = ScriptedToolLLM([
        AssistantTurn(tool_calls=[ToolCall("c1", "recall", {"query": "x"})]),
        AssistantTurn(content="好。"),
    ])
    resp = await _agent(llm, mem, [recall_tool(mem)], approval=approval).run("查一下")
    assert resp.reply == "好。"                            # 未被 confirm 阻塞


# ---------------------------------------------------------------- 安全加固(审计修复)

async def test_deny_policy_beats_safe_override(tmp_path):
    """安全边界:工具自称 safe(→auto)不得盖过显式 deny 策略。"""
    from core.approval import ApprovalQueue, Notifier
    from core.audit import AuditLog

    settings = ApprovalSettings(
        policies=[PolicyRule(action="blocked", when={}, level="deny")], default_level="auto")
    approval = ApprovalQueue(settings, AuditLog(tmp_path / "a.jsonl"), Notifier(settings))
    ran = []

    async def _r(args):
        ran.append(1)
        return "ok"
    tool = Tool("blocked", "", {"type": "object", "properties": {}}, _r, safe=True)  # 自称 safe
    llm = ScriptedToolLLM([AssistantTurn(tool_calls=[ToolCall("c", "blocked", {})]),
                           AssistantTurn(content="done")])
    await _agent(llm, FakeMemory(), [tool], approval=approval).run("go")
    assert ran == []                                   # deny 优先,safe 绕不过


def test_third_party_tool_cannot_self_elevate_to_auto():
    """安全边界:第三方 'tool' 插件即便自称 safe=True,build_toolbox 也强制 safe=False。"""
    from core.plugins import register
    from core.tools import build_toolbox

    async def _noop(args):
        return "x"
    register("tool", "evil")(
        lambda config: Tool("evil", "", {"type": "object", "properties": {}}, _noop, safe=True))
    tools = build_toolbox(load_config(agent={"tools": ["evil"]}), FakeMemory())
    evil = next(t for t in tools if t.name == "evil")
    assert evil.safe is False                          # 第三方不得自升级为自动放行


async def test_system_prompt_has_injection_defense():
    agent = _agent(ScriptedToolLLM([AssistantTurn(content="hi")]), FakeMemory(), [])
    sp = agent._system_prompt([])
    assert "不可信数据" in sp and "untrusted_web_content" in sp


async def test_per_turn_tool_call_cap():
    """单轮批量硬上限:一次塞入 20 个调用只执行前 8 个。"""
    ran = []

    async def _r(args):
        ran.append(1)
        return "x"
    tool = Tool("recall", "", {"type": "object", "properties": {}}, _r, safe=True)
    many = [ToolCall(f"c{i}", "recall", {}) for i in range(20)]
    llm = ScriptedToolLLM([AssistantTurn(tool_calls=many), AssistantTurn(content="done")])
    await _agent(llm, FakeMemory(), [tool]).run("go")
    assert len(ran) == 8


async def test_chat_tools_handles_dict_arguments():
    """有的供应商(litellm)返回的 arguments 已是 dict,不能被 json.loads 丢空。"""
    import httpx

    from adapters.llm import OpenAICompatAdapter
    from core.config import LLMRoleSettings

    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c", "type": "function",
             "function": {"name": "recall", "arguments": {"query": "猫"}}}]}}], "usage": {}})
    ad = OpenAICompatAdapter(LLMRoleSettings(base_url="http://x/v1", api_key="k", model="m"),
                             transport=httpx.MockTransport(handler))
    t = await ad.chat_tools([{"role": "user", "content": "q"}],
                            [{"type": "function", "function": {"name": "recall", "parameters": {}}}])
    assert t.tool_calls[0].arguments == {"query": "猫"}


# ---------------------------------------------------------------- 循环硬上限

async def test_loop_cap_forces_final_answer():
    mem = FakeMemory()

    async def _noop(args):
        return "又一次"
    tool = Tool("recall", "", {"type": "object", "properties": {}}, _noop, safe=True)
    # LLM 每步都要求调用工具,永不收敛
    llm = ScriptedToolLLM([AssistantTurn(tool_calls=[ToolCall("c", "recall", {})])])
    resp = await _agent(llm, mem, [tool], loops={"default_max_iterations": 2}).run("死循环")
    assert "loop_capped" in resp.reply                    # 触顶不静默,强制收尾


# ---------------------------------------------------------------- 工具箱组装

def test_build_toolbox_respects_config():
    mem = FakeMemory()
    assert [t.name for t in build_toolbox(load_config(agent={"tools": ["recall"]}), mem)] == ["recall"]
    # web_search 需真实搜索供应商(默认 none)→ 即便列出也不装
    cfg = load_config(agent={"tools": ["recall", "web_search", "web_fetch"]})
    from adapters.web import WebAdapter
    names = {t.name for t in build_toolbox(cfg, mem, web=WebAdapter(cfg.web))}
    assert names == {"recall", "web_fetch"}               # web_search 被跳过(provider=none)


# ---------------------------------------------------------------- services 装配

def test_services_wires_tool_agent_when_autonomy_tools():
    from fastapi.testclient import TestClient

    from services.api import create_app

    llm = ScriptedToolLLM([
        AssistantTurn(tool_calls=[ToolCall("c1", "recall", {"query": "猫"})]),
        AssistantTurn(content="你的猫叫 Benjamin。"),
    ])
    cfg = load_config(agent={"autonomy": "tools", "tools": ["recall", "remember"]},
                      embedder={"backend": "fake"}, vectordb={"mode": "memory"})
    app = create_app(cfg, llm=llm, memory=FakeMemory())
    with TestClient(app) as c:
        assert type(app.state.agent).__name__ == "ToolAgent"
        r = c.post("/chat", json={"message": "我的猫叫什么?", "session_id": "s"})
        assert r.status_code == 200 and "Benjamin" in r.json()["reply"]


def test_services_llm_wrapped_with_concurrency_semaphore():
    """M9.1 回归:服务入口自建 LLM 必须套并发信号量(此前只在 factory 装配路径有)。

    echo 后端无函数调用能力:包裹后 hasattr(chat_tools) 仍为 False → 正确落到 MemoryAgent。
    """
    from fastapi.testclient import TestClient

    from adapters.llm import ConcurrencyLimitedLLM
    from services.api import create_app

    cfg = load_config(llm={"mode": "echo"}, embedder={"backend": "fake"},
                      vectordb={"mode": "memory"})
    app = create_app(cfg)
    with TestClient(app):
        assert isinstance(app.state.llm, ConcurrencyLimitedLLM)
        assert not hasattr(app.state.llm, "chat_tools")
        assert type(app.state.agent).__name__ == "MemoryAgent"


async def test_openai_adapter_chat_tools_parsing():
    """真实 OpenAICompatAdapter.chat_tools 解析 function-calling(MockTransport,零外呼)。"""
    import json

    import httpx

    from adapters.llm import OpenAICompatAdapter
    from core.config import LLMRoleSettings

    def handler(req):
        if json.loads(req.content).get("tools"):
            return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "recall", "arguments": '{"query":"猫"}'}}]}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "答案"}}], "usage": {}})

    ad = OpenAICompatAdapter(LLMRoleSettings(base_url="http://x/v1", api_key="k", model="gpt-4o"),
                             transport=httpx.MockTransport(handler))
    t1 = await ad.chat_tools([{"role": "user", "content": "q"}],
                             [{"type": "function", "function": {"name": "recall", "parameters": {}}}])
    assert t1.tool_calls[0].name == "recall" and t1.tool_calls[0].arguments == {"query": "猫"}
    t2 = await ad.chat_tools([{"role": "user", "content": "q"}], [])
    assert not t2.tool_calls and t2.content == "答案"


def test_non_function_calling_model_falls_back_to_memory_agent():
    from fastapi.testclient import TestClient

    from services.api import create_app

    # 即便 autonomy=tools(现默认),echo 无 chat_tools → 安全回落 MemoryAgent(不崩)
    cfg = load_config(agent={"autonomy": "tools"}, embedder={"backend": "fake"},
                      vectordb={"mode": "memory"}, llm={"mode": "echo"})
    app = create_app(cfg, memory=FakeMemory())
    with TestClient(app) as c:
        assert type(app.state.agent).__name__ == "MemoryAgent"   # 无工具能力 → 记忆问答
        assert c.get("/healthz").status_code == 200


def test_autonomy_default_is_tools():
    assert load_config().agent.autonomy == "tools"                # 默认开
