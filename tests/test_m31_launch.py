"""M31 验收:一键运行(core.launch 的零配置 demo 回落 + 尊重用户配置 + CLI 接线)。

不真起 uvicorn(端到端起服务已在别处冒烟);这里锁定"双击即跑"的决策逻辑:
没配置 → 回落 demo 档;已配置(.env / MEMORY_AGENT_LLM__*)→ 不覆盖。
"""

from __future__ import annotations

import importlib


def _clean_env(monkeypatch):
    """把所有 MEMORY_AGENT_ 键(含 demo 档会写的那些)交给 monkeypatch 托管,
    确保 apply_demo 直接 os.environ.setdefault 写入的值在测试结束被清掉,不污染别的测试。"""
    import os

    import core.launch as L
    for k in list(os.environ):
        if k.startswith("MEMORY_AGENT_"):
            monkeypatch.delenv(k, raising=False)
    for k in L._DEMO_ENV:                             # demo 档要写的键,预先登记以便还原
        monkeypatch.delenv(k, raising=False)


def _fresh_launch():
    import core.launch as L
    return importlib.reload(L)


def test_demo_fallback_when_unconfigured(monkeypatch, tmp_path):
    # 清掉所有 MEMORY_AGENT_ 环境 + 切到无 .env 的空目录
    _clean_env(monkeypatch)
    monkeypatch.chdir(tmp_path)                       # 无 .env
    L = _fresh_launch()
    assert L._configured() is False
    assert L.apply_demo_defaults_if_unconfigured() is True
    import os
    assert os.environ["MEMORY_AGENT_LLM__MODE"] == "echo"
    assert os.environ["MEMORY_AGENT_EMBEDDER__BACKEND"] == "fake"
    assert os.environ["MEMORY_AGENT_AGENT__AUTONOMY"] == "chat"


def test_respects_user_env_config(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMORY_AGENT_LLM__MODE", "api")   # 用户已配
    L = _fresh_launch()
    assert L._configured() is True
    assert L.apply_demo_defaults_if_unconfigured() is False
    import os
    assert os.environ["MEMORY_AGENT_LLM__MODE"] == "api"  # 未被覆盖
    assert "MEMORY_AGENT_EMBEDDER__BACKEND" not in os.environ


def test_respects_dotenv_presence(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    (tmp_path / ".env").write_text("MEMORY_AGENT_LLM__MODE=api\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)                       # 有 .env → 视为已配置
    L = _fresh_launch()
    assert L._configured() is True
    assert L.apply_demo_defaults_if_unconfigured() is False


def test_force_demo_overrides_configured(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMORY_AGENT_LLM__MODE", "api")
    L = _fresh_launch()
    assert L.apply_demo_defaults_if_unconfigured(force_demo=True) is True   # --demo 强制


def test_cli_has_start_subcommand():
    from core.cli import build_parser

    args = build_parser().parse_args(
        ["start", "--no-browser", "--demo", "--host", "0.0.0.0", "--port", "9000"])
    assert args.cmd == "start" and args.no_browser is True and args.demo is True
    assert args.host == "0.0.0.0" and args.port == 9000     # RUN.md 里宣传的 --host/--port 真受理
