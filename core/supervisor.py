"""Supervisor(M25)——中心调度多 agent:协调者拆解任务、委派 worker、汇总结果。

与 M24 swarm 互补:swarm 是去中心化"手递手"(控制权转移给下一个成员);supervisor 有一个
中央协调者,把子任务**委派**给 worker 并**取回结果**汇总,控制权始终在协调者手里。

落地即"组合",不新造循环:
  - 每个 worker 就是一个 ToolAgent(带自己的人设 persona + 私有工具),默认不**自动**写回
    记忆(避免子任务污染;若显式给 worker 装 remember 工具则照常写)。
  - "委派"就是一个 Tool(core.tools.delegate_tool):run 里跑对应 worker、返回其回答。
  - 协调者本身也是一个 ToolAgent——它的"工具"正是这些 delegate_to_<worker>。
因此审批闸 / 循环硬上限 / 成本账 / 注入防御 全部自动复用;深度恒为 2(协调者→worker),
worker 不带 delegate 工具,无无限递归。对 delegate:<worker> 的 deny 策略会真正拦住该
worker 运行(运行 worker 就是审批闸的 execute 回调)。需 function-calling 模型;缺 worker 回落。
"""

from __future__ import annotations

from core.errors import LayerError
from core.harness import select_profile
from core.tool_agent import ToolAgent
from core.tools import build_toolbox, delegate_tool

_DEFAULT_SUPERVISOR_PROMPT = (
    "你是协调者。把用户任务拆解成子任务,用 delegate_to_* 工具委派给最合适的 worker,"
    "收齐结果后整合成面向用户的最终答复。不要自己臆造 worker 能给的信息;拿到足够结果后"
    "直接回答,不要无谓地反复委派。"
)


def _worker_runner(worker: ToolAgent):
    """把一个 worker ToolAgent 包成 async (task)->str,供 delegate_tool 调用。"""
    async def run(task: str) -> str:
        resp = await worker.run(task)
        return resp.reply
    return run


def build_supervisor(config, llm, memory, web=None, approval=None, simulator=None) -> ToolAgent:
    """按 config.supervisor 组装协调者 ToolAgent(工具=各 worker 的 delegate 工具)。
    装配期 fail-fast 校验:worker 非空、名字唯一。"""
    specs = config.supervisor.workers
    if not specs:
        raise LayerError("L3", "supervisor", "autonomy=supervisor 但 supervisor.workers 为空")
    names = [w.name for w in specs]
    if len(names) != len(set(names)):
        raise LayerError("L3", "supervisor", f"supervisor worker 名重复:{names}")

    profile = select_profile(config)
    delegates = []
    for w in specs:
        worker_tools = build_toolbox(config, memory, web, names=w.tools)
        worker = ToolAgent(llm, memory, config, approval=approval, tools=worker_tools,
                           profile=profile, persona=w.prompt or config.agent.system_prompt,
                           write_back=False, simulator=simulator)   # worker 不自动写回子任务
        delegates.append(delegate_tool(w.name, _worker_runner(worker)))

    return ToolAgent(llm, memory, config, approval=approval, tools=delegates,
                     profile=profile, simulator=simulator,
                     persona=config.supervisor.prompt or _DEFAULT_SUPERVISOR_PROMPT)
