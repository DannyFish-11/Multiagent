"""预检(doctor):跑起来之前,先把配置/依赖/目录一次性体检清楚。

给每一项 ✅/⚠️/❌ + 一句可照做的修复提示,让"配错了"在启动前就被指出来,
而不是等运行时抛一个难懂的错误。core/services 签名不改;只读检查,不改任何状态。

用:`memory-agent doctor` 或 `make doctor`。有 ❌ 时退出码非零(可进 CI/部署脚本)。
"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Check:
    level: str      # ok | warn | fail
    title: str
    detail: str = ""
    hint: str = ""


def _installed(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def _dir_writable(path: str) -> bool:
    """能否在此持久化(可创建/可写);只读判断,不实际创建目录。"""
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent          # 上溯到最近的已存在祖先
    return os.access(probe, os.W_OK)


def _load_registry():
    # import 各工厂模块以触发内置注册
    import adapters.cloud  # noqa: F401
    import adapters.embedder  # noqa: F401
    import adapters.llm  # noqa: F401
    import core.experiment  # noqa: F401
    import core.factory  # noqa: F401
    import core.harness  # noqa: F401 - 触发 profile 内置注册
    from core.plugins import REGISTRY

    return REGISTRY


def _check_swarm(config, mode: str) -> list[Check]:
    """swarm 成员配置预检:非空/名字唯一/entry 与 handoffs 均指向已定义成员。"""
    out: list[Check] = []
    members = config.swarm.members
    if not members:
        out.append(Check("fail", "autonomy=swarm 但 swarm.members 为空",
                         "无成员可流转,将回落记忆问答",
                         "配 swarm.members(见 docs/PLUGINS.md 的 swarm 段)或改 autonomy"))
        return out
    if mode not in ("api", "litellm"):
        out.append(Check("warn", f"autonomy=swarm 但 LLM={mode} 不支持 function-calling",
                         "echo/local 无工具调用能力,将回落记忆问答", "用 llm.mode=api 或 litellm"))
    names = [m.name for m in members]
    nameset = set(names)
    if len(names) != len(nameset):
        out.append(Check("fail", f"swarm 成员名重复:{names}", "", "成员 name 须唯一"))
    entry = config.swarm.entry or names[0]
    if entry not in nameset:
        out.append(Check("fail", f"swarm.entry '{entry}' 不在成员中", f"成员:{names}",
                         "把 entry 设为某个成员名"))
    dangling = {h for m in members for h in m.handoffs if h not in nameset}
    if dangling:
        out.append(Check("fail", f"swarm handoffs 指向未定义成员:{sorted(dangling)}",
                         "", "补上这些成员,或修正 handoffs"))
    selfloop = [m.name for m in members if m.name in m.handoffs]
    if selfloop:
        out.append(Check("fail", f"swarm 成员转交给自身:{selfloop}", "", "移除自指的 handoff"))
    if not any(m.handoffs for m in members):
        out.append(Check("warn", "swarm 无任何 handoff", "成员间不流转,等同单 agent",
                         "给至少一个成员配 handoffs"))
    if not [c for c in out if c.level == "fail"]:
        out.append(Check("ok", f"autonomy=swarm({len(members)} 成员,entry={entry})",
                         " → ".join(names)))
    return out


def _check_supervisor(config, mode: str) -> list[Check]:
    """supervisor worker 配置预检:非空 / 名字唯一 / 需 function-calling 模型。"""
    out: list[Check] = []
    workers = config.supervisor.workers
    if not workers:
        out.append(Check("fail", "autonomy=supervisor 但 supervisor.workers 为空",
                         "无 worker 可委派,将回落记忆问答",
                         "配 supervisor.workers(见 docs/PLUGINS.md)或改 autonomy"))
        return out
    if mode not in ("api", "litellm"):
        out.append(Check("warn", f"autonomy=supervisor 但 LLM={mode} 不支持 function-calling",
                         "echo/local 无工具调用能力,将回落记忆问答", "用 llm.mode=api 或 litellm"))
    names = [w.name for w in workers]
    if len(names) != len(set(names)):
        out.append(Check("fail", f"supervisor worker 名重复:{names}", "", "worker name 须唯一"))
    if not [c for c in out if c.level == "fail"]:
        out.append(Check("ok", f"autonomy=supervisor({len(workers)} worker)", " · ".join(names)))
    return out


def run_doctor(config) -> list[Check]:
    checks: list[Check] = []
    reg = _load_registry()

    # ---- LLM ----
    mode = config.llm.mode
    if mode not in reg.available("llm"):
        checks.append(Check("fail", f"LLM 插件 '{mode}' 未注册",
                            f"可用:{reg.available('llm')}",
                            "改 llm.mode 为已注册名,或装第三方插件包"))
    else:
        if mode == "api":
            if config.llm.chat.base_url and config.llm.chat.model:
                checks.append(Check("ok", f"LLM=api({config.llm.chat.model})"))
                if not config.llm.chat.api_key or config.llm.chat.api_key == "EMPTY":
                    checks.append(Check("warn", "LLM=api 未配 api_key",
                                        "多数云供应商需要;自建/兼容网关可留空",
                                        "填 MEMORY_AGENT_LLM__CHAT__API_KEY"))
            else:
                checks.append(Check("fail", "LLM=api 缺 base_url/model",
                                    "", "填 MEMORY_AGENT_LLM__CHAT__BASE_URL 与 __MODEL(make setup)"))
        elif mode == "litellm":
            if not config.llm.chat.model:
                checks.append(Check("fail", "LLM=litellm 缺 model", "",
                                    "填 llm.chat.model,如 anthropic/claude-3-5-sonnet"))
            elif not _installed("litellm"):
                checks.append(Check("fail", "LLM=litellm 但未装 litellm", "",
                                    "uv sync --extra litellm"))
            else:
                checks.append(Check("ok", f"LLM=litellm({config.llm.chat.model})"))
        elif mode == "echo":
            checks.append(Check("warn", "LLM=echo(demo 档)",
                                "只回显检索到的记忆,不做真实推理",
                                "真用请 make setup 换 api/litellm"))
        elif mode == "local":
            checks.append(Check("warn", "LLM=local(需本地 vLLM)",
                                f"须在 {config.llm.base_url} 起 vLLM(GPU)",
                                "无 GPU 就用 api/litellm/echo"))

    # ---- 自主工具循环(M22) ----
    if config.agent.autonomy == "tools":
        if mode in ("api", "litellm"):
            checks.append(Check("ok", f"autonomy=tools(工具:{config.agent.tools})"))
        else:
            checks.append(Check("warn", f"autonomy=tools 但 LLM={mode} 不支持 function-calling",
                                "echo/local 无工具调用能力,将回落记忆问答",
                                "用 llm.mode=api 或 litellm"))

    # ---- 去中心化 swarm(M24) ----
    if config.agent.autonomy == "swarm":
        checks.extend(_check_swarm(config, mode))

    # ---- 中心调度 supervisor(M25) ----
    if config.agent.autonomy == "supervisor":
        checks.extend(_check_supervisor(config, mode))

    # ---- Harness Profile(M23:按模型脚手架) ----
    from core.errors import LayerError
    from core.harness import effective_chat_model, select_profile

    want = config.agent.profile
    try:
        prof = select_profile(config)
        if want in ("auto", ""):
            model = effective_chat_model(config) or "(无模型名)"
            picked = prof.name if prof.name != "default" else "default(无专属脚手架)"
            checks.append(Check("ok", f"Harness profile=auto → {picked}",
                                f"按 chat 模型 '{model}' 匹配"))
        else:
            checks.append(Check("ok", f"Harness profile={prof.name}"))
    except LayerError as exc:
        checks.append(Check("fail", f"Harness profile '{want}' 未注册",
                            str(exc).split(";")[0],
                            "改 agent.profile 为 auto/none 或已注册名(make plugins 看 profile 类)"))

    # ---- 嵌入 ----
    backend = config.embedder.backend
    if backend not in reg.available("embedder"):
        checks.append(Check("fail", f"嵌入插件 '{backend}' 未注册",
                            f"可用:{reg.available('embedder')}"))
    elif backend == "jina_api":
        if config.embedder.jina_api_key:
            checks.append(Check("ok", "嵌入=jina_api(语义检索)"))
        else:
            checks.append(Check("fail", "嵌入=jina_api 缺 key", "",
                                "填 MEMORY_AGENT_EMBEDDER__JINA_API_KEY"))
    elif backend == "local":
        if _installed("torch") and _installed("transformers"):
            checks.append(Check("ok", "嵌入=local(本地 jina 模型)"))
        else:
            checks.append(Check("fail", "嵌入=local 缺依赖", "torch/transformers 未装",
                                "uv sync --extra local-embed(较重),或换 jina_api/fake"))
    elif backend == "fake":
        checks.append(Check("warn", "嵌入=fake(哈希嵌入)",
                            "词面重叠可检索,无语义,仅 demo", "真用请换 jina_api/local"))
    elif backend == "remote":
        checks.append(Check("ok", f"嵌入=remote({config.embedder.base_url})"))

    # ---- 向量库 ----
    vm = config.vectordb.mode
    if vm == "server":
        checks.append(Check("warn", f"向量库=server({config.vectordb.url})",
                            "需可达的 Qdrant", "docker: make up;或免 docker 用 vectordb.mode=local"))
    else:
        checks.append(Check("ok", f"向量库={vm}(进程内,免服务)"))
    if config.memory.backend not in reg.available("memory"):
        checks.append(Check("fail", f"记忆后端 '{config.memory.backend}' 未注册",
                            f"可用:{reg.available('memory')}"))

    # ---- 云供应商(仅当非默认/被用到时提示重依赖) ----
    prov = config.cloud.provider
    if prov not in {"local", "none"} and prov not in reg.available("cloud_provider"):
        checks.append(Check("fail", f"云供应商 '{prov}' 未注册",
                            f"可用:{reg.available('cloud_provider')}"))
    elif prov == "ray" and not _installed("ray"):
        checks.append(Check("fail", "cloud.provider=ray 但未装 ray", "", "uv sync --extra ray"))

    # ---- 授权令牌 + 来源可信(M30) ----
    if config.delegation.enabled:
        if config.delegation.permissions:
            ttl = config.delegation.ttl_s
            budget = config.delegation.max_budget_usd
            checks.append(Check("ok", "delegation 令牌已启用",
                                f"permissions={config.delegation.permissions} · "
                                f"预算={'∞' if budget <= 0 else budget} · "
                                f"时效={'不过期' if ttl <= 0 else f'{ttl}s'}"))
        else:
            checks.append(Check("fail", "delegation.enabled 但 permissions 为空",
                                "空许可 = 令牌下什么动作都做不了",
                                "配 delegation.permissions(如 [recall, remember, web_*])或关掉"))
    if config.approval.require_verified_source:
        checks.append(Check("ok", "来源可信闸已启用(provenance)",
                            f"需可信来源的动作:{config.approval.require_verified_source};"
                            f"可信来源:{config.approval.trusted_sources}"))

    # ---- 预算护栏 ----
    if config.budget.daily_usd and config.budget.daily_usd > 0:
        checks.append(Check("ok", f"日预算硬闸 ${config.budget.daily_usd}"))
    else:
        checks.append(Check("warn", "日预算=0", "成本闸门形同关闭",
                            "设 MEMORY_AGENT_BUDGET__DAILY_USD"))

    # ---- 目录可写(记忆/身份/审计要持久化 + 备份) ----
    for label, path in (("记忆/身份 data", config.identity.dir),
                        ("审计日志 logs", str(Path(config.approval.audit_path).parent))):
        if _dir_writable(path):
            checks.append(Check("ok", f"{label} 可写:{path}"))
        else:
            checks.append(Check("fail", f"{label} 不可写:{path}", "",
                                "换可写路径或修目录权限;生产上挂有备份的盘"))

    return checks


def render(checks: list[Check]) -> tuple[str, bool]:
    """渲染为文本报告 + 是否全部通过(无 fail)。"""
    icon = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}
    lines = ["memory-agent doctor —— 启动前体检", "=" * 48]
    for c in checks:
        lines.append(f"{icon[c.level]} {c.title}")
        if c.detail:
            lines.append(f"     {c.detail}")
        if c.hint and c.level != "ok":
            lines.append(f"     → {c.hint}")
    fails = [c for c in checks if c.level == "fail"]
    warns = [c for c in checks if c.level == "warn"]
    lines.append("=" * 48)
    if fails:
        lines.append(f"❌ {len(fails)} 项必须修复才能正常运行(另有 {len(warns)} 项提醒)。")
    elif warns:
        lines.append(f"✅ 可以运行(有 {len(warns)} 项提醒,多为 demo/降级档)。")
    else:
        lines.append("✅ 全部通过,放心跑。")
    return "\n".join(lines), not fails
