#!/usr/bin/env python
"""终端里和 memory-agent 对话(无需 GUI):连已在跑的 L3 API。

  make chat                          # 连 http://localhost:8002
  MEMORY_AGENT_API=http://host:8002 uv run python scripts/chat.py

命令:/quit 退出,/reset 换会话,/mem 显示上一轮命中的记忆。
"""

from __future__ import annotations

import os
import sys

import httpx

BASE = os.environ.get("MEMORY_AGENT_API", "http://localhost:8002").rstrip("/")


def main() -> int:
    session = "cli"
    last_mem: list[dict] = []
    print(f"memory-agent 对话 · {BASE}(/quit 退出,/reset 换会话,/mem 看命中记忆)")
    try:
        r = httpx.get(f"{BASE}/healthz", timeout=5)
        h = r.json()
        print(f"[healthz] {h.get('status')} · {h.get('layers')}")
    except Exception as exc:
        print(f"⚠️  连不上 {BASE}:{exc}\n   先 `make run-api` 或 `make quickstart` 起服务。",
              file=sys.stderr)
        return 1

    while True:
        try:
            msg = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0
        if not msg:
            continue
        if msg in ("/quit", "/exit"):
            return 0
        if msg == "/reset":
            session = "cli-" + os.urandom(3).hex()
            print(f"(已换到新会话 {session})")
            continue
        if msg == "/mem":
            print("上一轮命中记忆:", [m.get("content") for m in last_mem] or "(无)")
            continue
        try:
            resp = httpx.post(f"{BASE}/chat", json={"message": msg, "session_id": session},
                              timeout=180)
            if resp.status_code != 200:
                print(f"⚠️  HTTP {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
                continue
            body = resp.json()
        except Exception as exc:
            print(f"⚠️  请求失败:{exc}", file=sys.stderr)
            continue
        last_mem = body.get("memories_used", [])
        print(f"助手 > {body.get('reply', '')}")
        if last_mem:
            print(f"       (用到 {len(last_mem)} 条记忆,/mem 查看)")


if __name__ == "__main__":
    raise SystemExit(main())
