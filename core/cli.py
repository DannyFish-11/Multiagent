"""memory-agent 统一命令行入口(M21 产品化)。

装了本包后(`uv tool install .` / `pipx install .` / 仓库内 `uv run memory-agent`):

  memory-agent doctor      # 启动前体检:配置/依赖/目录一次看清
  memory-agent config      # 打印当前生效配置(密钥自动脱敏)
  memory-agent plugins     # 列出所有可用插件(内置 + 第三方)
  memory-agent run         # 起 L3 API(:8002)
  memory-agent chat        # 终端里对话(无需 GUI)
  memory-agent setup       # 首次运行向导:写 .env
  memory-agent demo        # 零 key 演示记忆闭环

一个命令覆盖上手全流程,便于交给任何人。
"""

from __future__ import annotations

import argparse
import os
import re
import runpy
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = PROJECT_ROOT / "scripts"

# 键名含以下词的非空值整体脱敏(webhook_url 本身即 bearer 秘密)
SECRET_HINT = ("key", "secret", "token", "password", "api_key", "webhook")
# 值里 URL 内嵌的 user:pass@ 凭据也脱敏(不影响普通 base_url 展示)
_URL_CRED = re.compile(r"://[^/@\s]+:[^/@\s]+@")


def _redact(obj):
    """递归脱敏:键名命中秘密词的非空值 → ***;字符串里 URL 内嵌凭据 → ://***@。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(h in k.lower() for h in SECRET_HINT) and v:
                out[k] = "***"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    if isinstance(obj, str):
        return _URL_CRED.sub("://***@", obj)
    return obj


def cmd_doctor(_args) -> int:
    from core.config import load_config
    from core.doctor import render, run_doctor

    report, ok = render(run_doctor(load_config()))
    print(report)
    return 0 if ok else 1


def cmd_config(_args) -> int:
    import json

    from core.config import load_config

    print(json.dumps(_redact(load_config().model_dump()), ensure_ascii=False, indent=2))
    return 0


def cmd_plugins(_args) -> int:
    return _runpy(SCRIPTS / "list_plugins.py")


def cmd_run(_args) -> int:
    # 复用 services.api 的 __main__(uvicorn 起服务)
    os.execvp(sys.executable, [sys.executable, "-m", "services.api"])
    return 0  # 不会到达


def cmd_start(args) -> int:
    # M31 一键运行:零配置也能跑 + 自动开浏览器(双击启动器 / 独立可执行都走这里)
    from core.launch import main as launch_main

    argv = []
    if getattr(args, "demo", False):
        argv.append("--demo")
    if getattr(args, "no_browser", False):
        argv.append("--no-browser")
    if getattr(args, "host", None):
        argv += ["--host", args.host]
    if getattr(args, "port", None):
        argv += ["--port", str(args.port)]
    return launch_main(argv)


def cmd_chat(_args) -> int:
    return _runpy(SCRIPTS / "chat.py")


def cmd_setup(args) -> int:
    argv = ["--demo"] if getattr(args, "demo", False) else []
    return _runpy(SCRIPTS / "setup.py", argv)


def cmd_demo(_args) -> int:
    os.environ.setdefault("MEMORY_AGENT_LLM__MODE", "echo")
    os.environ.setdefault("MEMORY_AGENT_EMBEDDER__BACKEND", "fake")
    os.environ.setdefault("MEMORY_AGENT_VECTORDB__MODE", "memory")
    os.environ.setdefault("MEMORY_AGENT_MEMORY__EXTRACTION", "verbatim")
    return _runpy(SCRIPTS / "demo.py")


def _runpy(path: Path, argv: list[str] | None = None) -> int:
    if not path.exists():
        print(f"找不到 {path.name}(该子命令需在仓库目录内运行)。", file=sys.stderr)
        return 1
    old = sys.argv
    sys.argv = [str(path), *(argv or [])]
    try:
        runpy.run_path(str(path), run_name="__main__")
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = old


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memory-agent",
                                description="有长期记忆的 AI agent —— 一个命令走完上手全流程")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="启动前体检:配置/依赖/目录")
    sub.add_parser("config", help="打印生效配置(密钥脱敏)")
    sub.add_parser("plugins", help="列出所有可用插件")
    sub.add_parser("run", help="起 L3 API(:8002)")
    st = sub.add_parser("start", help="一键运行:起服务并自动打开浏览器(零配置即 demo 档)")
    st.add_argument("--demo", action="store_true", help="强制 demo 档(echo+fake,零 key)")
    st.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    st.add_argument("--host", default=None, help="监听地址(默认 127.0.0.1,仅本机)")
    st.add_argument("--port", type=int, default=None, help="端口(默认取 config.services.api_port)")
    sub.add_parser("chat", help="终端对话")
    sp = sub.add_parser("setup", help="首次运行向导:写 .env")
    sp.add_argument("--demo", action="store_true", help="非交互写零 key demo 档")
    sub.add_parser("demo", help="零 key 演示记忆闭环")
    return p


HANDLERS = {
    "doctor": cmd_doctor, "config": cmd_config, "plugins": cmd_plugins,
    "run": cmd_run, "start": cmd_start, "chat": cmd_chat, "setup": cmd_setup, "demo": cmd_demo,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return HANDLERS[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
