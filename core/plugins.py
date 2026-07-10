"""统一插件注册表(M21):把各类后端做成"按名字注册、掉进去就用"的插件。

设计目标(耦合/解耦 + 模块化拓展):业务码只按**名字**取后端,不认识具体实现;
新增一个 LLM/嵌入/记忆/云供应商/任务源/工具,只需注册一个名字——树内一行装饰器,
树外 pip 装个包即被自动发现,无需改本仓库。

扩展点(kind):
  llm / embedder / memory / cloud_provider / task_source / tool

两种注册方式:
1. 树内:``@register("llm", "myname")`` 装饰工厂函数/类(import 该模块即登记)。
2. 树外(第三方包):在其 pyproject 声明 entry point group ``memory_agent.plugins``:
       [project.entry-points."memory_agent.plugins"]
       "llm:mycloud" = "mypkg.llm:build_mycloud"     # 名字 "kind:name" → 工厂
       my_bundle     = "mypkg:register"              # 无冒号 → 回调 register(registry)
   首次访问某 kind 时经 importlib.metadata 自动发现加载。

工厂签名(各 kind 统一,便于 build_* 统一调用):
  llm(config, role, ledger) -> LLMClient
  embedder(settings, ledger) -> Embedder
  memory(config, embedder, llm) -> MemoryStore
  cloud_provider(config) -> CloudProvider
  task_source(spec, seed) -> 具 .stream()->list[Task] 的对象
  tool(config) -> 工具对象(经审批策略引擎治理)
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from core.errors import LayerError

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "memory_agent.plugins"
KINDS = ("llm", "embedder", "memory", "cloud_provider", "task_source", "tool")


class PluginRegistry:
    """线程安全的按 (kind, name) 索引的插件表 + entry_points 懒发现。"""

    def __init__(self) -> None:
        self._plugins: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._discovered = False

    # ---- 注册 ----

    def add(self, kind: str, name: str, obj: Any) -> None:
        """登记一个插件(非装饰器)。同名后注册者覆盖前者(便于用户覆写内置)。"""
        with self._lock:
            self._plugins.setdefault(kind, {})[name] = obj

    def register(self, kind: str, name: str) -> Callable[[Any], Any]:
        """装饰器:``@register("llm", "echo")``。返回被装饰对象原样(不改变它)。"""
        def deco(obj: Any) -> Any:
            self.add(kind, name, obj)
            return obj
        return deco

    # ---- entry_points 发现(树外插件) ----

    def _discover(self) -> None:
        if self._discovered:
            return
        with self._lock:
            if self._discovered:
                return
            self._discovered = True
            try:
                from importlib.metadata import entry_points
                eps = entry_points(group=ENTRY_POINT_GROUP)
            except Exception as exc:  # 发现失败绝不拖垮主流程
                logger.debug("entry_points 发现跳过: %s", exc)
                return
            for ep in eps:
                try:
                    target = ep.load()
                except Exception as exc:
                    logger.warning("插件 entry point 加载失败 %r: %s", ep.name, exc)
                    continue
                if ":" in ep.name:
                    kind, _, name = ep.name.partition(":")
                    self.add(kind, name, target)
                    logger.info("发现第三方插件 %s:%s", kind, name)
                elif callable(target):
                    try:
                        target(self)  # 回调式:自行注册多个
                    except Exception as exc:
                        logger.warning("插件注册回调失败 %r: %s", ep.name, exc)

    # ---- 解析 ----

    def get(self, kind: str, name: str) -> Any:
        self._discover()
        table = self._plugins.get(kind, {})
        if name not in table:
            raise LayerError(
                "L-plugin", "registry",
                f"未注册的 {kind} 插件 {name!r};已注册:{sorted(table)}。"
                f"第三方插件经 entry_points group '{ENTRY_POINT_GROUP}'(如 "
                f"\"{kind}:{name}\" = \"pkg.mod:factory\")掉入即用。",
            )
        return table[name]

    def create(self, kind: str, name: str, *args: Any, **kwargs: Any) -> Any:
        """取出工厂并调用(工厂签名见模块 docstring)。"""
        return self.get(kind, name)(*args, **kwargs)

    def available(self, kind: str) -> list[str]:
        self._discover()
        return sorted(self._plugins.get(kind, {}))

    def snapshot(self) -> dict[str, list[str]]:
        """当前所有已注册插件(含发现的)。用于 /plugins 端点、诊断。"""
        self._discover()
        return {k: sorted(v) for k, v in sorted(self._plugins.items())}


# 进程级单例 + 便捷别名
REGISTRY = PluginRegistry()
register = REGISTRY.register
get_plugin = REGISTRY.get
create_plugin = REGISTRY.create
available = REGISTRY.available
