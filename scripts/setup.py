#!/usr/bin/env python
"""memory-agent 首次运行向导:交互式生成 .env,零手改配置即可上手。

  uv run python scripts/setup.py          # 交互式(make setup)
  uv run python scripts/setup.py --demo   # 非交互:写零 key demo 档

写出的 .env 用 MEMORY_AGENT_* 覆盖 config.yaml(优先级:进程环境 > .env > config.yaml)。
密钥只落 .env(已 gitignore),镜像与仓库都不含任何密钥。
"""

from __future__ import annotations

import sys
from getpass import getpass
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# 云端 OpenAI 兼容供应商预设(base_url, 默认模型);custom 让用户自填
PROVIDERS = {
    "1": ("DeepSeek", "https://api.deepseek.com", "deepseek-chat"),
    "2": ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
    "3": ("Moonshot/Kimi", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "4": ("自定义(任一 OpenAI 兼容端点)", "", ""),
}


def _ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        sys.exit(1)
    return val or default


def _ask_secret(prompt: str) -> str:
    try:
        return getpass(f"{prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        sys.exit(1)


def _choose(prompt: str, options: dict[str, str], default: str) -> str:
    print(prompt)
    for k, label in options.items():
        print(f"  {k}) {label}")
    return _ask("选择", default)


def _write_env(lines: list[str]) -> None:
    if ENV_PATH.exists():
        ans = _ask(f"{ENV_PATH.name} 已存在,覆盖?(原文件备份为 .env.bak)[y/N]", "N")
        if ans.lower() != "y":
            print("未改动。")
            sys.exit(0)
        ENV_PATH.rename(ENV_PATH.with_suffix(".bak"))
    header = [
        "# memory-agent 配置(scripts/setup.py 生成)。密钥只在此文件,已 gitignore。",
        "# 覆盖规则:MEMORY_AGENT_ 前缀 + __ 嵌套。改完 make run-api / make quickstart 生效。",
        "",
    ]
    ENV_PATH.write_text("\n".join(header + lines) + "\n", encoding="utf-8")
    print(f"\n✅ 已写入 {ENV_PATH}")


def _demo_lines() -> list[str]:
    return [
        "MEMORY_AGENT_LLM__MODE=echo",
        "MEMORY_AGENT_EMBEDDER__BACKEND=fake",
        "MEMORY_AGENT_VECTORDB__MODE=memory",
        "MEMORY_AGENT_MEMORY__EXTRACTION=verbatim",
    ]


def run_demo() -> None:
    _write_env(_demo_lines())
    print("零 key demo 档:make run-api 起服务 → make chat 对话(或 make demo 一键演示)。")
    print("⚠️  哈希嵌入 + echo 回显,仅验证链路,不代表真实效果。")


def run_interactive() -> None:
    print("=" * 64)
    print("memory-agent 首次运行向导")
    print("=" * 64)

    mode = _choose(
        "\n怎么用?",
        {
            "1": "云端 API(推荐:加 1-2 把 key,不碰 GPU/docker 也能跑)",
            "2": "零 key demo(先看效果;检索质量退化,仅验证链路)",
            "3": "本地模型(需 GPU;走 config.yaml 默认本地路径)",
        },
        default="1",
    )

    if mode == "2":
        run_demo()
        return
    if mode == "3":
        _write_env([
            "MEMORY_AGENT_LLM__MODE=local",
            "# 本地路径:见 README 路径 C —— detect_hardware 定档 + download_models + make up-gpu",
        ])
        print("本地模型档:按 README「路径 C —— 完整目标机器」定档、下模型、起 vLLM。")
        return

    # ---- 云端 API ----
    lines = ["MEMORY_AGENT_LLM__MODE=api"]
    pick = _choose("\nLLM 供应商(任一 OpenAI 兼容端点):",
                   {k: v[0] for k, v in PROVIDERS.items()}, default="1")
    name, base_url, model = PROVIDERS.get(pick, PROVIDERS["4"])
    if not base_url:
        base_url = _ask("端点 base_url(如 https://api.deepseek.com)")
    model = _ask(f"{name} 模型名", model or "")
    key = _ask_secret(f"{name} API key(不回显)")
    lines += [
        f"MEMORY_AGENT_LLM__CHAT__BASE_URL={base_url}",
        f"MEMORY_AGENT_LLM__CHAT__API_KEY={key}",
        f"MEMORY_AGENT_LLM__CHAT__MODEL={model}",
    ]

    emb = _choose(
        "\n嵌入(记忆检索用):",
        {
            "1": "Jina 云 API(推荐:真实语义检索,另需一把 Jina key)",
            "2": "先不接嵌入 key(退化哈希嵌入,词面重叠可检索,无语义)",
        },
        default="1",
    )
    if emb == "1":
        jkey = _ask_secret("Jina API key(不回显)")
        lines += [
            "MEMORY_AGENT_EMBEDDER__BACKEND=jina_api",
            f"MEMORY_AGENT_EMBEDDER__JINA_API_KEY={jkey}",
        ]
    else:
        lines.append("MEMORY_AGENT_EMBEDDER__BACKEND=fake")

    stack = _choose(
        "\n运行方式:",
        {"1": "Docker(推荐:make quickstart 起 API+Qdrant)", "2": "本地进程(免 docker)"},
        default="1",
    )
    if stack == "2":
        # 本地进程:向量库用进程内落盘文件,免 Qdrant/docker
        lines.append("MEMORY_AGENT_VECTORDB__MODE=local")
    # Docker 路径下向量库由 compose 指向 qdrant 容器(config 默认 server),无需在 .env 设

    budget = _ask("\n日预算上限(美元,成本硬闸;超了直接拒付)", "5")
    lines.append(f"MEMORY_AGENT_BUDGET__DAILY_USD={budget}")

    _write_env(lines)
    print("\n下一步:")
    if stack == "1":
        print("  make quickstart     # 起 Docker 全栈(API + Qdrant),等健康检查通过")
    else:
        print("  make run-api        # 本地起 API(:8002)")
    print("  make chat           # 终端里和它对话")


def main() -> int:
    if "--demo" in sys.argv:
        run_demo()
        return 0
    if not sys.stdin.isatty():
        print("非交互终端:请加 --demo 写零 key demo 档,或在交互式终端运行 make setup。",
              file=sys.stderr)
        return 1
    run_interactive()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
