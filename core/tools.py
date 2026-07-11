"""工具抽象与工具箱(M22)——把 agent 的"手"标准化。

一个 Tool = 名字 + 描述 + 参数 JSON schema + 一个 async run(args)。ToolAgent 把这些
渲染成 OpenAI function-calling 规格交给 LLM,LLM 决定调哪个;执行时经审批闸(M9.2)
放行/待批/拒绝,并受循环硬上限(M14.1)约束。第三方工具可经插件注册表("tool" 类)
掉进来。内置工具:recall/remember(记忆,安全直放)、web_search/web_fetch(上网,按
config 审批分级)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

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


_BUILTINS = {"recall": recall_tool, "remember": remember_tool,
             "web_search": web_search_tool, "web_fetch": web_fetch_tool}


def build_toolbox(config, memory, web=None) -> list[Tool]:
    """按 config.agent.tools 组装启用的工具(内置 + 第三方 'tool' 插件)。"""
    enabled = list(config.agent.tools)
    tools: list[Tool] = []
    for name in enabled:
        if name in ("recall", "remember"):
            tools.append(_BUILTINS[name](memory))
        elif name in ("web_search", "web_fetch") and web is not None:
            # web_search 还需真实搜索供应商;web_fetch 只要 web adapter
            if name == "web_search" and config.web.search_provider == "none":
                continue
            tools.append(_BUILTINS[name](web))
    # 第三方工具插件:factory(config) -> Tool
    from core.plugins import REGISTRY

    for name in REGISTRY.available("tool"):
        if name in enabled:
            try:
                tools.append(REGISTRY.get("tool", name)(config))
            except Exception:
                pass
    return tools


def _first_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return str(content or "")
