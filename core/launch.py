"""一键运行(M31)——双击即用:零配置也能跑,起服务并自动打开浏览器聊天页。

面向"不想装环境/不想敲命令行"的用户:
  - 双击启动器(run.bat / run.sh)或独立可执行文件(PyInstaller,见 memory-agent.spec)都走这里;
  - **零配置友好**:没有 .env 且未显式配 LLM 时,自动回落 demo 档(echo + fake 嵌入 + 内存向量库),
    零 key / 零 GPU / 零 docker 立刻能聊(体验记忆闭环);想用真实大模型就放个 .env(memory-agent
    setup 生成)或设环境变量,启动器会尊重你的配置、不覆盖;
  - 启动后**自动打开浏览器**到聊天页(webui);无图形界面(服务器/CI)则跳过,不报错。
"""

from __future__ import annotations

import os
import sys
import threading
import webbrowser

_DEMO_ENV = {
    "MEMORY_AGENT_LLM__MODE": "echo",
    "MEMORY_AGENT_EMBEDDER__BACKEND": "fake",
    "MEMORY_AGENT_VECTORDB__MODE": "memory",
    "MEMORY_AGENT_MEMORY__BACKEND": "qdrant",
    "MEMORY_AGENT_MEMORY__EXTRACTION": "verbatim",
    "MEMORY_AGENT_AGENT__AUTONOMY": "chat",
}


def _configured() -> bool:
    """用户是否已显式配置 LLM(有 .env 或设了 MEMORY_AGENT_LLM__ 环境变量)。"""
    if any(k.startswith("MEMORY_AGENT_LLM__") or k == "MEMORY_AGENT_CONFIG" for k in os.environ):
        return True
    return os.path.exists(".env")


def apply_demo_defaults_if_unconfigured(force_demo: bool = False) -> bool:
    """未配置时注入 demo 档环境变量(不覆盖用户已设的键)。返回是否进入 demo 档。"""
    if not force_demo and _configured():
        return False
    for k, v in _DEMO_ENV.items():
        os.environ.setdefault(k, v)
    return True


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="memory-agent-start", description="一键起 memory-agent 并打开浏览器聊天页")
    p.add_argument("--host", default="127.0.0.1", help="监听地址(默认仅本机)")
    p.add_argument("--port", type=int, default=None, help="端口(默认取 config.services.api_port)")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.add_argument("--demo", action="store_true", help="强制 demo 档(echo+fake+内存库,零 key)")
    args = p.parse_args(argv)

    demo = apply_demo_defaults_if_unconfigured(force_demo=args.demo)

    from core.config import load_config

    cfg = load_config()
    port = args.port if args.port is not None else cfg.services.api_port   # --port 0(临时端口)也尊重
    url = f"http://{args.host}:{port}"

    print(f"memory-agent 启动中… {'(demo 档:零 key/零 GPU)' if demo else ''}")
    print(f"  聊天页:{url}   (按 Ctrl+C 停止)")
    if not demo:
        print("  已读取你的配置(.env / 环境变量)。")
    else:
        print("  想用真实大模型:放一个 .env(memory-agent setup 生成)后重启。")

    if not args.no_browser:
        # 延迟 1.5s 等服务起来再开浏览器;无 GUI 环境 webbrowser 静默失败,不影响服务
        def _open() -> None:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Timer(1.5, _open).start()

    import uvicorn

    from services.api import create_app

    uvicorn.run(create_app(cfg), host=args.host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
