"""工具抽象与工具箱(M22)——把 agent 的"手"标准化。

一个 Tool = 名字 + 描述 + 参数 JSON schema + 一个 async run(args)。ToolAgent 把这些
渲染成 OpenAI function-calling 规格交给 LLM,LLM 决定调哪个;执行时经审批闸(M9.2)
放行/待批/拒绝,并受循环硬上限(M14.1)约束。第三方工具可经插件注册表("tool" 类)
掉进来。内置工具:recall/remember(记忆,安全直放)、web_search/web_fetch(上网,按
config 审批分级)。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from core.schemas import MultimodalInput


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class AssistantTurn:
    """LLM 一步的产物:要么给出文本(final),要么要求调用一批工具。"""
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict                                  # JSON schema
    run: Callable[[dict], Awaitable[str]]
    action: str = ""                                  # 审批动作名(默认=name)
    safe: bool = False                                # True → 审批 level_override=auto
    handoff_to: str = ""                              # 非空 → 这是转交工具(M24 swarm),
    #                                                   调用即把控制权交给该 named 成员

    def spec(self) -> dict:
        """OpenAI function-calling 规格。"""
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


# ---------------------------------------------------------------- 内置工具

def recall_tool(memory) -> Tool:
    async def run(args: dict) -> str:
        hits = await memory.search(MultimodalInput.text(args["query"]), k=int(args.get("k", 5)))
        return json.dumps([{"content": h.content} for h in hits], ensure_ascii=False)
    return Tool("recall", "检索长期记忆里与查询相关的内容(先想想以前记过什么)",
                {"type": "object", "properties": {
                    "query": {"type": "string", "description": "要检索的关键词/问题"},
                    "k": {"type": "integer", "description": "返回条数,默认 5"}},
                 "required": ["query"]},
                run, safe=True)


def remember_tool(memory) -> Tool:
    async def run(args: dict) -> str:
        mid = await memory.add(MultimodalInput.text(args["text"]), {"source": "tool"})
        return f"已记住(id={mid})"
    return Tool("remember", "把一条值得长期记住的信息存入记忆",
                {"type": "object", "properties": {
                    "text": {"type": "string", "description": "要记住的自包含陈述"}},
                 "required": ["text"]},
                run, safe=True)


def web_search_tool(web) -> Tool:
    async def run(args: dict) -> str:
        res = await web.search(args["query"], k=int(args.get("k", 5)))
        return json.dumps(res, ensure_ascii=False)
    return Tool("web_search", "联网搜索(返回标题/链接/摘要)",
                {"type": "object", "properties": {"query": {"type": "string"}},
                 "required": ["query"]},
                run, action="web_search")


def web_fetch_tool(web) -> Tool:
    async def run(args: dict) -> str:
        r = await web.fetch(args["url"])
        return r["untrusted"]                          # 已包裹为不可信数据块(防注入)
    return Tool("web_fetch", "抓取一个网页的正文",
                {"type": "object", "properties": {"url": {"type": "string"}},
                 "required": ["url"]},
                run, action="web_fetch")


# M30 ②:只有**可信代码**可注入的元字段;LLM 生成的工具参数里一律剥除,防其自证数据来源
# 绕过 provenance 闸(_source 必须由系统/可信上游打标,不能由模型在 tool_call 参数里声称)。
_RESERVED_META = ("_source",)


def sanitize_tool_args(args: dict) -> dict:
    """剥除 LLM 工具参数里的可信元字段(_source 等)。非 dict 原样返回。"""
    if not isinstance(args, dict):
        return args
    return {k: v for k, v in args.items() if k not in _RESERVED_META}


def handoff_tool(target: str) -> Tool:
    """转交工具(M24 swarm):LLM 调用它即把控制权手递手交给 named 成员 target。
    去中心化——由当前成员自主决定交给谁,无中央调度器。safe=True(转交本身是内部
    路由,不触外部动作;经审计但默认直放),受 swarm 转接链上限约束防 A↔B 乒乓。"""
    async def run(args: dict) -> str:
        return f"已转交给 {target}"
    return Tool(
        f"transfer_to_{target}",
        f"把当前任务转交给「{target}」成员处理;在 reason 里附上给对方的上下文。",
        {"type": "object", "properties": {
            "reason": {"type": "string", "description": "转交原因 / 给接手成员的上下文"}}},
        run, action=f"handoff:{target}", safe=True, handoff_to=target)


def delegate_tool(worker_name: str, run_worker) -> Tool:
    """委派工具(M25 supervisor):协调者调用它把子任务交给 worker 执行并**取回结果**。
    与 swarm 的 handoff 不同——控制权不转移,worker 只把结果返给协调者汇总。safe=True
    (委派本身经审计;若对 delegate:<worker> 配 deny 策略则真正拦住,因为运行 worker 就是
    审批闸的 execute 回调;worker 内部工具再各自过审批闸)。"""
    async def run(args: dict) -> str:
        return await run_worker(args.get("task", ""))
    return Tool(
        f"delegate_to_{worker_name}",
        f"把一个子任务交给「{worker_name}」处理并拿回它的结果(你负责汇总)。",
        {"type": "object", "properties": {
            "task": {"type": "string", "description": f"交给 {worker_name} 的具体子任务"}},
         "required": ["task"]},
        run, action=f"delegate:{worker_name}", safe=True)


_BUILTINS = {"recall": recall_tool, "remember": remember_tool,
             "web_search": web_search_tool, "web_fetch": web_fetch_tool}


def build_toolbox(config, memory, web=None, names=None) -> list[Tool]:
    """按工具名组装工具(内置 + 第三方 'tool' 插件)。names 缺省用 config.agent.tools;
    swarm 成员传各自的 names 组装私有工具箱。"""
    enabled = list(config.agent.tools if names is None else names)
    tools: list[Tool] = []
    for name in enabled:
        if name in ("recall", "remember"):
            tools.append(_BUILTINS[name](memory))
        elif name in ("web_search", "web_fetch") and web is not None:
            # web_search 还需真实搜索供应商;web_fetch 只要 web adapter
            if name == "web_search" and config.web.search_provider == "none":
                continue
            tools.append(_BUILTINS[name](web))
    # 第三方工具插件:factory(config) -> Tool。安全边界:第三方工具**不得**自升级为
    # 自动放行(safe 强制 False),一律经审批分级——防 pip 装个插件就绕过治理。
    from core.plugins import REGISTRY

    for name in REGISTRY.available("tool"):
        if name in enabled:
            try:
                t = REGISTRY.get("tool", name)(config)
                t.safe = False
                tools.append(t)
            except Exception:
                # 别静默吞:算子显式启用的工具构造失败要能看见(否则 agent 当它不存在)
                logging.getLogger(__name__).warning(
                    "第三方工具 %r 构造失败,已跳过(检查其 config/依赖)", name, exc_info=True)
    return tools
