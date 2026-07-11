"""Harness Profiles(M23)——按模型打包"脚手架",让开源模型发挥真实水平。

问题:同一套为闭源旗舰调好的"脚手架"(系统提示 + 采样参数 + 工具循环处理),换到
开源模型(GLM / DeepSeek / Kimi / 本地 gemma 等)上往往只发挥一半实力——不是模型不行,
是外围没针对它调。Harness Profile 把每个模型的调优参数打包成一个**命名、可切换**的
Profile,随模型一起选。

Profile 是一等插件("profile" 类,走 core/plugins 注册表):树内 ``@register("profile", ...)``
一行,树外 pip 装个包经 entry_points ``profile:xxx`` 掉入即用——和 llm/embedder 等后端
**同一套机制,不另造轮子**。工厂签名 ``profile() -> HarnessProfile``(无参)。

选择(select_profile 按 ``config.agent.profile``):
  - ``auto``(默认):按当前 chat 模型名匹配内置 profile;**匹配不到回落 default(零侵入,
    保持原行为)**。闭源旗舰(gpt/claude/gemini…)默认不匹配 → 走 default,不动其表现。
  - 具体名(如 ``glm``):直接取该 profile;未注册则报错并列出可用。
  - ``default`` / ``none``:显式无脚手架。

作用点(纯加法,不改协议签名):
  - system_prompt:叠加到用户 ``agent.system_prompt`` 之后的模型专属指引(MemoryAgent 与
    ToolAgent 都用)。
  - sampling / max_tools_per_turn / max_steps / tool_result_max_chars:仅在 ToolAgent 工具
    循环生效(采样参数经 ``**kw`` 透传给 chat_tools;结果截断即文章①"只回传有用结果"的
    轻量落地,省 token、防长结果塞爆上下文)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.plugins import get_plugin, register

# 工具使用纪律:多数开源模型要么过度调用工具、要么该调不调。一句克制的提示显著稳住行为。
_TOOL_DISCIPLINE = (
    "## 工具使用纪律\n"
    "能直接回答就不要调用工具;确需外部信息时才调,且尽量用最少的调用完成任务——"
    "多个相互独立的调用可在同一步一次性发起。拿到足够信息后立即给出最终回答。"
)


@dataclass
class HarnessProfile:
    """一个模型的脚手架配置。所有字段都是"加法/覆盖",空/None 表示不改默认行为。"""

    name: str
    # auto 选择:chat 模型名(转小写)含其中任一子串即命中该 profile
    match: tuple[str, ...] = ()
    # 叠加到用户 system_prompt 之后的模型专属指引(空则不叠加)
    system_prompt: str = ""
    # 采样参数(如 {"temperature": 0.3}),经 **kw 透传给 chat_tools(空则用端点默认)
    sampling: dict = field(default_factory=dict)
    # 单轮工具批量上限覆盖(None → 用 ToolAgent 全局默认)
    max_tools_per_turn: int | None = None
    # 工具循环步数上限覆盖(None → 用 config.loops)
    max_steps: int | None = None
    # 工具结果回灌前的截断字符数(None → 不截断);小上下文模型防长结果塞爆
    tool_result_max_chars: int | None = None


# ---------------------------------------------------------------- 内置 profile

@register("profile", "default")
def _p_default() -> HarnessProfile:
    """零脚手架:保持框架原生行为(闭源旗舰 / 未知模型的 auto 回落档)。"""
    return HarnessProfile(name="default")


@register("profile", "gemma")
def _p_gemma() -> HarnessProfile:
    """本地 gemma(小上下文、算力有限):更确定的采样 + 结果截断,减负担。"""
    return HarnessProfile(
        name="gemma", match=("gemma",),
        system_prompt=_TOOL_DISCIPLINE + "\n回答简洁直接,不铺陈无关内容。",
        sampling={"temperature": 0.3},
        tool_result_max_chars=2000,
    )


@register("profile", "glm")
def _p_glm() -> HarnessProfile:
    """智谱 GLM 系(工具/代码能力强):中低温度稳住工具决策。"""
    return HarnessProfile(
        name="glm", match=("glm",),
        system_prompt=_TOOL_DISCIPLINE,
        sampling={"temperature": 0.4},
        tool_result_max_chars=4000,
    )


@register("profile", "deepseek")
def _p_deepseek() -> HarnessProfile:
    """DeepSeek 系:低温度提升工具调用与推理的一致性。"""
    return HarnessProfile(
        name="deepseek", match=("deepseek",),
        system_prompt=_TOOL_DISCIPLINE,
        sampling={"temperature": 0.3},
        tool_result_max_chars=4000,
    )


@register("profile", "kimi")
def _p_kimi() -> HarnessProfile:
    """月之暗面 Kimi / Moonshot(长上下文):放宽结果截断,充分利用长窗口。"""
    return HarnessProfile(
        name="kimi", match=("kimi", "moonshot", "k2"),
        system_prompt=_TOOL_DISCIPLINE,
        sampling={"temperature": 0.4},
        tool_result_max_chars=8000,
    )


@register("profile", "qwen")
def _p_qwen() -> HarnessProfile:
    """通义千问 Qwen 系。"""
    return HarnessProfile(
        name="qwen", match=("qwen",),
        system_prompt=_TOOL_DISCIPLINE,
        sampling={"temperature": 0.4},
        tool_result_max_chars=4000,
    )


# ---------------------------------------------------------------- 选择

def effective_chat_model(config) -> str:
    """当前生效的 chat 模型名(用于 auto 匹配)。api/litellm 取 llm.chat.model,
    local 取 llm.model,echo 无模型名。"""
    mode = config.llm.mode
    if mode in ("api", "litellm"):
        return config.llm.chat.model or ""
    if mode == "local":
        return config.llm.model or ""
    return ""


def select_profile(config) -> HarnessProfile:
    """按 config.agent.profile 选一个 HarnessProfile(见模块 docstring)。"""
    name = (getattr(config.agent, "profile", "auto") or "auto").strip()
    if name in ("auto", ""):
        model = effective_chat_model(config).lower()
        if model:
            from core.plugins import REGISTRY

            for pname in REGISTRY.available("profile"):
                if pname == "default":
                    continue
                prof = get_plugin("profile", pname)()
                if prof.match and any(tok in model for tok in prof.match):
                    return prof
        return get_plugin("profile", "default")()
    if name == "none":
        return HarnessProfile(name="none")
    return get_plugin("profile", name)()   # 未注册 → LayerError,列出可用
