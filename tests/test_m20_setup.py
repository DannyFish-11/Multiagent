"""M20 turnkey:首次运行向导(scripts/setup.py)生成 .env 的正确性。

不依赖 tty:monkeypatch input/getpass 驱动交互分支,断言写出的 .env 键值。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent / "scripts" / "setup.py"


def _load(monkeypatch, tmp_path, answers, secrets):
    """加载 setup 模块,把 .env 指到 tmp,喂入预设回答/密钥序列。"""
    spec = importlib.util.spec_from_file_location("setup_wizard", SETUP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ENV_PATH", tmp_path / ".env")
    it_ans = iter(answers)
    it_sec = iter(secrets)
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(it_ans))
    monkeypatch.setattr(mod, "getpass", lambda *a, **k: next(it_sec))
    return mod


def _parse(env_text: str) -> dict[str, str]:
    out = {}
    for line in env_text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def test_demo_profile(tmp_path, monkeypatch):
    mod = _load(monkeypatch, tmp_path, answers=[], secrets=[])
    mod.run_demo()
    env = _parse((tmp_path / ".env").read_text(encoding="utf-8"))
    assert env["MEMORY_AGENT_LLM__MODE"] == "echo"
    assert env["MEMORY_AGENT_EMBEDDER__BACKEND"] == "fake"
    assert env["MEMORY_AGENT_VECTORDB__MODE"] == "memory"
    assert env["MEMORY_AGENT_MEMORY__EXTRACTION"] == "verbatim"


def test_cloud_api_with_jina_and_local_stack(tmp_path, monkeypatch):
    # 回答序列:模式=1(云) → 供应商=1(DeepSeek) → 模型(回车用默认) →
    #           嵌入=1(Jina) → 运行方式=2(本地进程) → 预算=3
    answers = ["1", "1", "", "1", "2", "3"]
    secrets = ["sk-llm-xxx", "jina-yyy"]
    mod = _load(monkeypatch, tmp_path, answers, secrets)
    mod.run_interactive()
    env = _parse((tmp_path / ".env").read_text(encoding="utf-8"))
    assert env["MEMORY_AGENT_LLM__MODE"] == "api"
    assert env["MEMORY_AGENT_LLM__CHAT__BASE_URL"] == "https://api.deepseek.com"
    assert env["MEMORY_AGENT_LLM__CHAT__MODEL"] == "deepseek-chat"
    assert env["MEMORY_AGENT_LLM__CHAT__API_KEY"] == "sk-llm-xxx"
    assert env["MEMORY_AGENT_EMBEDDER__BACKEND"] == "jina_api"
    assert env["MEMORY_AGENT_EMBEDDER__JINA_API_KEY"] == "jina-yyy"
    assert env["MEMORY_AGENT_VECTORDB__MODE"] == "local"   # 本地进程档免 Qdrant
    assert env["MEMORY_AGENT_BUDGET__DAILY_USD"] == "3"


def test_cloud_api_fake_embed_docker_stack(tmp_path, monkeypatch):
    # 云 + 自定义端点 + fake 嵌入 + docker 运行方式(不写 vectordb,交给 compose)
    answers = ["1", "4", "https://api.example.com/v1", "my-model", "2", "1", "5"]
    secrets = ["sk-custom"]
    mod = _load(monkeypatch, tmp_path, answers, secrets)
    mod.run_interactive()
    env = _parse((tmp_path / ".env").read_text(encoding="utf-8"))
    assert env["MEMORY_AGENT_LLM__CHAT__BASE_URL"] == "https://api.example.com/v1"
    assert env["MEMORY_AGENT_LLM__CHAT__MODEL"] == "my-model"
    assert env["MEMORY_AGENT_EMBEDDER__BACKEND"] == "fake"
    assert "MEMORY_AGENT_EMBEDDER__JINA_API_KEY" not in env
    assert "MEMORY_AGENT_VECTORDB__MODE" not in env       # docker 档由 compose 指向 qdrant


def test_env_written_is_loadable_by_config(tmp_path, monkeypatch):
    """向导写出的 .env 能被 load_config 读到(端到端:向导 → 配置)。"""
    mod = _load(monkeypatch, tmp_path, answers=[], secrets=[])
    mod.run_demo()
    from core.config import AppConfig
    cfg = AppConfig(_env_file=str(tmp_path / ".env"))
    assert cfg.llm.mode == "echo"
    assert cfg.embedder.backend == "fake"
