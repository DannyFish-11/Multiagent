"""邮件→记忆管道(M6.1 EmailMemoryIngest)。

只做按需吸取:仅当用户显式要求处理某封邮件时调用;不做全量自动吸取
(附录 B 非目标:隐私与噪音均不可控)。
抽取四类记忆:承诺(promise)/偏好(preference)/关系(relationship)/事实(fact),
来源标注 source=gmail + message_id。
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from core.errors import LayerError
from core.schemas import Message, MultimodalInput

if TYPE_CHECKING:
    from adapters.llm import LLMClient
    from adapters.memory import MemoryStore

CATEGORIES = ("promise", "preference", "relationship", "fact")

EMAIL_EXTRACTION_SYSTEM = """\
你是邮件记忆抽取器。从给定邮件中抽取值得长期记住的内容,分四类:
- promise:承诺/待办/截止时间(谁答应了什么、何时)
- preference:偏好(收发件人表达的喜好、习惯)
- relationship:人际关系(谁是谁、什么角色、什么关系)
- fact:其他关键事实
输出严格 JSON:{"promise": [...], "preference": [...], "relationship": [...], "fact": [...]},
每个元素为一条自包含的中文陈述句。无内容的类给空数组。不要输出 JSON 以外的字符。"""


class EmailMemoryIngest:
    def __init__(self, llm: "LLMClient", memory: "MemoryStore") -> None:
        self._llm = llm
        self._memory = memory

    async def ingest(self, email_text: str, message_id: str,
                     session_id: str = "gmail") -> dict[str, list[str]]:
        """处理一封用户显式要求处理的邮件,返回各类入库的记忆内容。"""
        raw = await self._llm.chat(
            [Message(role="system", content=EMAIL_EXTRACTION_SYSTEM),
             Message(role="user", content=email_text)],
            temperature=0.0,
        )
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            raise LayerError("L2", "email-ingest", f"抽取输出非 JSON: {raw[:200]}")
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as exc:
            raise LayerError("L2", "email-ingest", f"抽取解析失败: {raw[:200]}") from exc

        stored: dict[str, list[str]] = {}
        for category in CATEGORIES:
            items = data.get(category) or []
            if not isinstance(items, list):
                raise LayerError("L2", "email-ingest", f"类别 {category} 非数组: {items!r}")
            stored[category] = []
            for item in items:
                await self._memory.add(
                    MultimodalInput.text(str(item)),
                    {"source": "gmail", "message_id": message_id,
                     "category": category, "session_id": session_id},
                )
                stored[category].append(str(item))
        return stored
