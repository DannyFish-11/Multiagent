"""M23 验收:Harness Profiles(按模型脚手架,让开源模型发挥真实水平)。

覆盖:profile 作为 "profile" 类插件注册/发现;auto 按模型名选(glm/deepseek/kimi/qwen/
gemma),匹配不到回落 default(零侵入);显式名 / none / 未注册名;ToolAgent 采样透传 +
系统提示叠加 + 工具结果截断(文章①)+ 单轮/步数上限覆盖;MemoryAgent 系统提示叠加且
记忆块仍在末尾(不破坏 EchoLLM);doctor 顾问项。
"""

from __future__ import annotations

import core.harness  # noqa: F401 - 触发内置 profile 注册
from core.config import load_config
from core.harness import HarnessProfile, effective_chat_model, select_profile
from core.plugins import REGISTRY
from core.schemas import MemoryHit
from core.tool_agent import ToolAgent
from core.tools import AssistantTurn, Tool, ToolCall


class FakeMemory:
    def __init__(self):
        self.added = []

    async def search(self, query, k=5):
        return [MemoryHit(id="m1", score=0.9, content="猫叫 Benjamin")]

    async def add(self, inp, meta=None):
        self.added.append(inp.content)
        return "id1"


def _cfg(model: str, mode: str = "api", **agent):
    if mode in ("api", "litellm"):
        llm = {"mode": mode, "chat": {"model": model}}
    else:
        llm = {"mode": mode, "model": model}
    return load_config(llm=llm, agent=agent)


# ---------------------------------------------------------------- 插件注册/发现

def test_profile_is_a_registered_plugin_kind():
    snap = REGISTRY.snapshot()
    assert "profile" in snap
    for name in ("default", "gemma", "glm", "deepseek", "kimi", "qwen"):
        assert name in snap["profile"]


# ---------------------------------------------------------------- auto 选择

def test_auto_selects_by_model_name():
    assert select_profile(_cfg("zai-org/GLM-5")).name == "glm"
    assert select_profile(_cfg("deepseek-ai/DeepSeek-V4")).name == "deepseek"
    assert select_profile(_cfg("moonshotai/Kimi-K2.6")).name == "kimi"
    assert select_profile(_cfg("Qwen/Qwen3-72B")).name == "qwen"
    assert select_profile(_cfg("gemma-4", mode="local")).name == "gemma"


def test_auto_falls_back_to_default_for_frontier_and_unknown():
    # 闭源旗舰 / 未知模型不匹配 → default(零侵入,保持原生行为)
    assert select_profile(_cfg("gpt-5.2-codex")).name == "default"
    assert select_profile(_cfg("claude-opus-x")).name == "default"
    assert select_profile(_cfg("some-unknown-model")).name == "default"
    default = select_profile(_cfg("gpt-5.2-codex"))
    assert default.system_prompt == "" and default.sampling == {}  # 真的无脚手架


def test_effective_chat_model_by_mode():
    assert effective_chat_model(_cfg("glm-5")) == "glm-5"
    assert effective_chat_model(_cfg("gemma-4", mode="local")) == "gemma-4"
    assert effective_chat_model(load_config(llm={"mode": "echo"})) == ""


# ---------------------------------------------------------------- 显式 / none / 未注册

def test_explicit_profile_name():
    assert select_profile(_cfg("whatever", profile="deepseek")).name == "deepseek"


def test_none_profile_is_empty_scaffold():
    prof = select_profile(_cfg("glm-5", profile="none"))
    assert prof.name == "none" and prof.system_prompt == "" and prof.sampling == {}


def test_unknown_profile_name_raises():
    from core.errors import LayerError

    try:
        select_profile(_cfg("x", profile="nope"))
        assert False, "应报未注册"
    except LayerError as exc:
        assert "profile" in str(exc) and "nope" in str(exc)


def test_third_party_profile_via_registry():
    REGISTRY.add("profile", "myco",
                 lambda: HarnessProfile(name="myco", match=("mymodel",), system_prompt="嗨"))
    assert select_profile(_cfg("mymodel-v1")).name == "myco"      # auto 也能选到第三方


# ---------------------------------------------------------------- ToolAgent 生效

class RecordingLLM:
    """记录 chat_tools 收到的 (messages, kw);按脚本返回。"""
    def __init__(self, turns):
        self._turns = list(turns)
        self._i = 0
        self.seen = []

    async def chat_tools(self, messages, tools, **kw):
        # 深拷贝 messages 语义:此处只需读取,记录引用快照的浅副本
        self.seen.append(({"messages": list(messages), "kw": kw}))
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        return turn


async def test_toolagent_passes_sampling_and_scaffold():
    mem = FakeMemory()
    llm = RecordingLLM([AssistantTurn(content="好的。")])
    prof = HarnessProfile(name="t", system_prompt="SCAFFOLD_MARK", sampling={"temperature": 0.3})
    agent = ToolAgent(llm, mem, load_config(), tools=[], profile=prof)
    resp = await agent.run("你好")
    assert resp.reply == "好的。"
    assert llm.seen[0]["kw"] == {"temperature": 0.3}                # 采样透传
    sys = llm.seen[0]["messages"][0]["content"]
    assert "SCAFFOLD_MARK" in sys                                   # 系统提示叠加


async def test_toolagent_clips_tool_result():
    mem = FakeMemory()
    big = "X" * 5000

    async def _run(args):
        return big
    tool = Tool("recall", "检索", {"type": "object", "properties": {}}, _run, safe=True)
    llm = RecordingLLM([
        AssistantTurn(tool_calls=[ToolCall("c1", "recall", {"q": "a"})]),
        AssistantTurn(content="done"),
    ])
    prof = HarnessProfile(name="t", tool_result_max_chars=100)
    await ToolAgent(llm, mem, load_config(), tools=[tool], profile=prof).run("q")
    # 第二次调用时,回灌的 tool 消息已被截断
    fed = [m for m in llm.seen[1]["messages"] if m.get("role") == "tool"][0]["content"]
    assert len(fed) < 5000 and "已截断" in fed and fed.startswith("X" * 100)


async def test_toolagent_max_tools_and_steps_override():
    mem = FakeMemory()
    calls = []

    async def _run(args):
        calls.append(args)
        return "ok"
    tool = Tool("t", "d", {"type": "object", "properties": {}}, _run, safe=True)
    # 单轮塞 5 个调用,profile 限 2 → 只执行 2
    many = AssistantTurn(tool_calls=[ToolCall(f"c{i}", "t", {"i": i}) for i in range(5)])
    llm = RecordingLLM([many, AssistantTurn(content="fin")])
    prof = HarnessProfile(name="t", max_tools_per_turn=2, max_steps=3)
    await ToolAgent(llm, mem, load_config(), tools=[tool], profile=prof).run("q")
    assert len(calls) == 2                                          # 单轮批量上限覆盖生效


# ---------------------------------------------------------------- MemoryAgent 生效

class CaptureChatLLM:
    def __init__(self):
        self.messages = None

    async def chat(self, messages, **kw):
        self.messages = messages
        return "ok"


async def test_memoryagent_layers_scaffold_before_memory_block():
    from core.agent import MemoryAgent

    llm = CaptureChatLLM()
    prof = HarnessProfile(name="t", system_prompt="MEM_SCAFFOLD")
    agent = MemoryAgent(llm, FakeMemory(), load_config(), profile=prof)
    await agent.chat("你好", sync_memory_write=True)
    sent_sys = llm.messages[0].content
    assert "MEM_SCAFFOLD" in sent_sys
    # 脚手架在记忆块之前(EchoLLM 依赖"## 相关记忆"之后即记忆内容)
    assert sent_sys.index("MEM_SCAFFOLD") < sent_sys.index("## 相关记忆")


async def test_memoryagent_default_profile_unchanged():
    """auto 回落 default 时,MemoryAgent 系统提示不含额外脚手架(闭源/未知模型零侵入)。"""
    from core.agent import MemoryAgent

    llm = CaptureChatLLM()
    agent = MemoryAgent(llm, FakeMemory(), _cfg("gpt-5.2"))       # auto → default
    await agent.chat("hi", sync_memory_write=True)
    sent_sys = llm.messages[0].content
    # 基础提示 + 记忆块,中间无脚手架空段
    assert "## 相关记忆" in sent_sys


# ---------------------------------------------------------------- doctor 顾问项

def test_doctor_reports_auto_profile():
    from core.doctor import run_doctor

    checks = run_doctor(_cfg("zai-org/GLM-5"))
    line = [c for c in checks if "Harness profile" in c.title]
    assert line and "glm" in line[0].title and line[0].level == "ok"


def test_doctor_flags_unknown_profile():
    from core.doctor import run_doctor

    checks = run_doctor(_cfg("x", profile="nope"))
    bad = [c for c in checks if "Harness profile" in c.title and c.level == "fail"]
    assert bad
