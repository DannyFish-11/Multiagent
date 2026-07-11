#!/usr/bin/env python
"""列出所有已注册插件(内置 + 第三方 entry_points 发现)。用法:make plugins"""

from __future__ import annotations


def main() -> int:
    # import 各工厂模块以触发内置注册
    import adapters.cloud  # noqa: F401
    import adapters.embedder  # noqa: F401
    import adapters.llm  # noqa: F401
    import core.experiment  # noqa: F401
    import core.factory  # noqa: F401
    import core.harness  # noqa: F401 - 触发 profile 内置注册
    from core.plugins import ENTRY_POINT_GROUP, KINDS, REGISTRY

    snap = REGISTRY.snapshot()   # 触发 entry_points 发现
    print("已注册插件(内置 + 第三方 entry_points):\n")
    for kind in KINDS:
        names = snap.get(kind, [])
        print(f"  {kind:15s} {', '.join(names) if names else '(无)'}")
    print(f"\n第三方插件经 entry_points group '{ENTRY_POINT_GROUP}' 掉入,如:")
    print('  [project.entry-points."memory_agent.plugins"]')
    print('  "llm:mycloud" = "mypkg.llm:build_mycloud"')
    print("\n写插件见 docs/PLUGINS.md。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
