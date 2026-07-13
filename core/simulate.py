"""Gecko-lite 预执行模拟(M32)——危险动作真实执行前先"预演":参数校验 + 效果预览。

灵感取自 Gecko(ICML'26)的 Argument Validator / Response Generator / Task Feedback,
但只保留与本项目安全底座咬合的两点,且默认零 LLM 成本、零模拟漂移:

  1. 参数校验(规则层,确定性、默认开):required/type/enum 不符 → **不执行**,把纠错
     反馈回灌给 LLM 让它自行修正(Gecko 的"失败-反馈-重试"闭环,安全版:非法调用不落地)。
  2. 效果预览(默认确定性):工具自报 preview 优先;否则按 schema 生成"将以参数 X 执行"
     的摘要。预览随 confirm 动作附给人类批准者——把"盲批"升级为"看清后果再批"。

可选增强(默认关,需 LLM、有 token 成本与判断漂移风险,显式开启才用):
  - 语义校验:schema 合法但语义不对(如 ticker 传公司全名而非 AAPL)。
  - LLM 效果估计:工具无自报预览时,用 LLM 估计副作用(标注"估计",仅供参考)。

设计取舍:确定性、零成本的规则校验 + 预览默认开;要花钱、可能"自信地错"的 LLM 环节默认关
——一个"自信的错误模拟"比"没有模拟"更危险(会误导批准人与模型)。校验器自身故障一律
fail-open(放行),不因判断器抖动误伤合法调用;安全由 auto/confirm/deny 与硬闸兜底。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from core.schemas import Message

logger = logging.getLogger(__name__)


@dataclass
class Assessment:
    """一次预执行评估的结果。"""
    ok: bool                    # 参数校验是否通过(False → 不执行,把 reason 回灌 LLM 重试)
    reason: str = ""            # 不通过原因(校验反馈);通过时为 ""
    effect: str = ""            # 该动作会造成什么(供人类批准参考);read/safe 动作可为空
    estimated: bool = False     # effect 是否为 LLM 估计(vs 确定性预览)


def _type_ok(value, jtype: str) -> bool:
    """JSON schema 基础类型校验。bool 是 int 子类,数值类型须排除 bool。"""
    if jtype == "string":
        return isinstance(value, str)
    if jtype == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if jtype == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if jtype == "boolean":
        return isinstance(value, bool)
    if jtype == "object":
        return isinstance(value, dict)
    if jtype == "array":
        return isinstance(value, list)
    return True   # 未知/未声明类型不拦(宁可少拦,不可误伤)


def validate_rules(schema: dict, args: dict) -> tuple[bool, str]:
    """确定性 JSON-schema 子集校验:required / type / enum。返回 (ok, reason)。

    只做高置信、低误报的检查;未知字段不拦(不同工具对额外字段容忍度不一,硬拦易误伤)。"""
    if not isinstance(args, dict):
        return False, f"参数须为 JSON 对象,收到 {type(args).__name__}"
    schema = schema if isinstance(schema, dict) else {}
    props = schema.get("properties", {})
    props = props if isinstance(props, dict) else {}
    problems: list[str] = []
    for req in schema.get("required", []) or []:
        if req not in args:
            problems.append(f"缺少必填字段 {req!r}")
    for key, val in args.items():
        spec = props.get(key)
        if not isinstance(spec, dict):
            continue
        jtype = spec.get("type")
        if jtype and not _type_ok(val, jtype):
            problems.append(f"字段 {key!r} 类型应为 {jtype},收到 {type(val).__name__}")
        enum = spec.get("enum")
        if enum is not None and val not in enum:
            problems.append(f"字段 {key!r} 应为 {enum} 之一,收到 {val!r}")
    return (not problems), ";".join(problems)


def _summarize_args(args, limit: int = 200) -> str:
    try:
        s = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(args)
    return s if len(s) <= limit else s[:limit] + "…"


def _parse_json_obj(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise ValueError("无 JSON 对象")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("JSON 非对象")
    return data


_SEMANTIC_SYSTEM = (
    "你是工具调用的参数语义审查员。给定工具描述、参数 schema、本次参数、以及用户任务,"
    "判断参数在**语义上**是否合理(格式合法但语义不对也算不合理,如 ticker 字段传公司全名"
    "'Apple Inc.' 而非 'AAPL')。不评判该不该调用,只看参数取值是否对得上字段意图。"
    '输出严格 JSON:{"ok": true/false, "reason": "不合理时一句话说明并给出正确取值方向"}。')

_PREVIEW_SYSTEM = (
    "你是工具执行的效果预览器。给定工具描述、参数 schema、本次参数、以及用户任务,"
    "用一句中文说明**这次调用若真实执行会造成什么**(尤其是不可逆的改动性副作用:发信/"
    "删档/下单/改预订等)。只描述后果,不要评判、不要复述参数。输出纯文本一句话。")


class Simulator:
    """预执行评估器:注入 ToolAgent/SwarmAgent 的工具循环。

    llm 仅在开启语义校验 / LLM 预览时使用;默认二者皆关,则纯确定性、零额外调用。"""

    def __init__(self, settings, llm=None) -> None:
        self._settings = settings
        self._llm = llm

    async def assess(self, tool, args: dict, task: str = "") -> Assessment:
        """执行前评估:先规则校验(不过即止),再可选语义校验,最后对改动性动作生成预览。"""
        if self._settings.enabled:
            ok, reason = validate_rules(getattr(tool, "parameters", {}), args)
            if not ok:
                return Assessment(ok=False, reason=reason)
            if self._settings.semantic_validation and self._llm is not None:
                ok, reason = await self._validate_semantic(tool, args, task)
                if not ok:
                    return Assessment(ok=False, reason=reason)
        # 效果预览:仅对改动性 / 非 safe(会走审批分级)动作生成;read/safe 免预览省开销
        if getattr(tool, "mutating", False) or not getattr(tool, "safe", False):
            effect, estimated = await self._preview(tool, args, task)
            return Assessment(ok=True, effect=effect, estimated=estimated)
        return Assessment(ok=True)

    async def _preview(self, tool, args, task) -> tuple[str, bool]:
        cap = self._settings.preview_max_chars
        preview_fn = getattr(tool, "preview", None)
        if preview_fn is not None:                       # 工具自报确定性预览优先
            try:
                text = await preview_fn(args)
                return str(text)[:cap], False
            except Exception:
                logger.warning("工具 %s 自报预览失败,回落通用摘要",
                               getattr(tool, "name", "?"), exc_info=True)
        if self._settings.llm_preview and self._llm is not None:   # LLM 效果估计(可选)
            est = await self._llm_preview(tool, args, task)
            if est:
                return est[:cap], True
        return f"将执行 {getattr(tool, 'name', '?')},参数:{_summarize_args(args)}"[:cap], False

    async def _validate_semantic(self, tool, args, task) -> tuple[bool, str]:
        user = (f"工具:{getattr(tool, 'name', '')} — {getattr(tool, 'description', '')}\n"
                f"参数 schema:{json.dumps(getattr(tool, 'parameters', {}), ensure_ascii=False)}\n"
                f"本次参数:{_summarize_args(args, 400)}\n"
                f"用户任务:{task or '(未提供)'}")
        try:
            raw = await self._llm.chat(
                [Message(role="system", content=_SEMANTIC_SYSTEM),
                 Message(role="user", content=user)], temperature=0.0)
            data = _parse_json_obj(raw)
            return bool(data.get("ok", True)), str(data.get("reason", ""))
        except Exception:
            logger.warning("语义校验调用/解析失败,放行(fail-open,不误伤合法调用)",
                           exc_info=True)
            return True, ""      # 校验器故障不应拦下合法调用;安全由审批分级 + 硬闸兜底

    async def _llm_preview(self, tool, args, task) -> str:
        user = (f"工具:{getattr(tool, 'name', '')} — {getattr(tool, 'description', '')}\n"
                f"参数 schema:{json.dumps(getattr(tool, 'parameters', {}), ensure_ascii=False)}\n"
                f"本次参数:{_summarize_args(args, 400)}\n"
                f"用户任务:{task or '(未提供)'}")
        try:
            raw = await self._llm.chat(
                [Message(role="system", content=_PREVIEW_SYSTEM),
                 Message(role="user", content=user)], temperature=0.0)
            return raw.strip()
        except Exception:
            logger.warning("LLM 效果预览失败,回落通用摘要", exc_info=True)
            return ""
