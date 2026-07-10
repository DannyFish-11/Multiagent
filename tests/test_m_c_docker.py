"""PHASE2.5 M-C 结构验收:Docker 产物(镜像构建/端到端 up 属目标机器 verify_25.sh)。"""

from __future__ import annotations

import subprocess

import yaml

from tests.conftest import PROJECT_ROOT


def test_compose_three_services_and_healthchecks():
    compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    # 默认清单三个服务;vllm 仅 gpu profile(本地路径保留)
    assert {"qdrant", "memory-api", "mcp-server"} <= set(services)
    assert services["vllm"]["profiles"] == ["gpu"]
    assert services["mcp-server"]["profiles"] == ["mcp"]
    # 每个服务有 healthcheck(memory-api 的在镜像 HEALTHCHECK 内)
    for name in ("qdrant", "mcp-server", "vllm"):
        assert "healthcheck" in services[name], name
    assert "HEALTHCHECK" in (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    # 记忆数据/日志/导出挂 volume(容器可销毁重建)
    api_vols = " ".join(services["memory-api"]["volumes"])
    for mount in ("/app/data", "/app/logs", "/app/exports"):
        assert mount in api_vols, mount
    # qdrant 数据挂 ./data/qdrant
    assert any("./data/qdrant" in v for v in services["qdrant"]["volumes"])
    # 密钥经 .env 注入,compose 明文环境变量里不得出现 key/secret 值
    env = services["memory-api"].get("environment", {})
    assert ".env" in str(services["memory-api"]["env_file"])
    assert not any("KEY" in k and v for k, v in env.items() if isinstance(v, str) and v)


def test_dockerfile_slim_multistage_no_secrets():
    df = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert df.count("FROM python:3.12-slim") == 2  # 多阶段
    assert "--extra local-embed" not in df          # 安装命令不打包 torch/模型权重
    assert "API_KEY" not in df and "SECRET" not in df.upper().replace("NO_SECRETS", "")
    assert "USER agent" in df                       # 非 root 运行
    # .dockerignore 排除数据与密钥
    di = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    for entry in (".env", "data/", ".git"):
        assert entry in di, entry


def test_env_example_covers_required_keys():
    env = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    for key in (
        "MEMORY_AGENT_LLM__MODE",
        "MEMORY_AGENT_LLM__CHAT__BASE_URL",
        "MEMORY_AGENT_LLM__CHAT__API_KEY",
        "MEMORY_AGENT_LLM__CHAT__MODEL",
        "MEMORY_AGENT_LLM__MEMORY__BASE_URL",
        "MEMORY_AGENT_EMBEDDER__JINA_API_KEY",
        "MEMORY_AGENT_BUDGET__DAILY_USD",
        "MEMORY_AGENT_VECTORDB__URL",
    ):
        assert key in env, key
    # 模板不含任何真实密钥值
    for line in env.splitlines():
        if "API_KEY" in line and not line.strip().startswith("#"):
            assert line.strip().endswith("="), f"密钥模板必须留空: {line}"


def test_bootstrap_and_verify_scripts():
    for name in ("bootstrap.sh", "verify_25.sh"):
        path = PROJECT_ROOT / "scripts" / name
        assert path.exists()
        subprocess.run(["bash", "-n", str(path)], check=True)  # 语法有效
    verify = (PROJECT_ROOT / "scripts" / "verify_25.sh").read_text(encoding="utf-8")
    # 七项验收全部在场
    for marker in ("①", "②", "③", "④", "⑤", "⑥", "⑦"):
        assert marker in verify, marker


def test_env_override_reaches_api_mode(tmp_path, monkeypatch):
    """.env 注入路径:环境变量能把 llm 切到 api 模式并填入三元组。"""
    monkeypatch.setenv("MEMORY_AGENT_LLM__MODE", "api")
    monkeypatch.setenv("MEMORY_AGENT_LLM__CHAT__BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("MEMORY_AGENT_LLM__CHAT__API_KEY", "sk-test")
    monkeypatch.setenv("MEMORY_AGENT_LLM__CHAT__MODEL", "m-test")
    from core.config import load_config

    cfg = load_config()
    assert cfg.llm.mode == "api"
    assert cfg.llm.chat.base_url == "https://api.example.com/v1"
    assert cfg.llm.chat.model == "m-test"
