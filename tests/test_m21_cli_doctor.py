"""M21 产品化验收:doctor 预检 + memory-agent CLI + /config·/plugins 路由脱敏。"""

from __future__ import annotations

from core.cli import _redact, main
from core.config import load_config
from core.doctor import render, run_doctor


# ---------------------------------------------------------------- doctor 预检

def test_doctor_demo_profile_passes():
    cfg = load_config(llm={"mode": "echo"}, embedder={"backend": "fake"},
                      vectordb={"mode": "memory"})
    checks = run_doctor(cfg)
    _report, ok = render(checks)
    assert ok is True                                  # 无 ❌ → 可运行
    assert not [c for c in checks if c.level == "fail"]


def test_doctor_api_missing_key_fails():
    cfg = load_config(llm={"mode": "api"}, embedder={"backend": "fake"},
                      vectordb={"mode": "memory"})   # api 缺 base_url/model
    checks = run_doctor(cfg)
    _report, ok = render(checks)
    assert ok is False
    assert any(c.level == "fail" and "api" in c.title for c in checks)


def test_doctor_jina_missing_key_fails():
    cfg = load_config(llm={"mode": "echo"}, embedder={"backend": "jina_api"},
                      vectordb={"mode": "memory"})
    checks = run_doctor(cfg)
    assert any(c.level == "fail" and "jina" in c.title for c in checks)


def test_doctor_unknown_plugin_name_fails():
    cfg = load_config(llm={"mode": "nonesuch"}, embedder={"backend": "fake"},
                      vectordb={"mode": "memory"})
    checks = run_doctor(cfg)
    assert any(c.level == "fail" and "nonesuch" in c.title for c in checks)


# ---------------------------------------------------------------- 脱敏

def test_redact_hides_secrets():
    red = _redact({
        "api_key": "sk-xxx", "secret_key": "s", "password": "p", "token": "t",
        "model": "gpt-4o", "nested": {"jina_api_key": "j", "base_url": "u"}, "empty_key": "",
    })
    assert red["api_key"] == "***" and red["secret_key"] == "***"
    assert red["password"] == "***" and red["token"] == "***"
    assert red["model"] == "gpt-4o" and red["nested"]["base_url"] == "u"
    assert red["nested"]["jina_api_key"] == "***"
    assert red["empty_key"] == ""                      # 空值不脱敏(仍是空)


# ---------------------------------------------------------------- CLI 调度

def test_cli_doctor_exit_codes(monkeypatch, capsys):
    # demo 档 → 0
    for k, v in {"MEMORY_AGENT_LLM__MODE": "echo", "MEMORY_AGENT_EMBEDDER__BACKEND": "fake",
                 "MEMORY_AGENT_VECTORDB__MODE": "memory"}.items():
        monkeypatch.setenv(k, v)
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "体检" in out


def test_cli_config_redacts(monkeypatch, capsys):
    monkeypatch.setenv("MEMORY_AGENT_LLM__MODE", "api")
    monkeypatch.setenv("MEMORY_AGENT_LLM__CHAT__API_KEY", "sk-should-not-appear")
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "sk-should-not-appear" not in out and '"***"' in out


def test_cli_plugins_runs(capsys):
    assert main(["plugins"]) == 0
    assert "llm" in capsys.readouterr().out


# ---------------------------------------------------------------- 诊断路由

def test_config_and_plugins_routes_redacted():
    from fastapi.testclient import TestClient

    from services.api import create_app

    cfg = load_config(llm={"mode": "echo", "chat": {"api_key": "sk-secret"}},
                      embedder={"backend": "fake"}, vectordb={"mode": "memory"})
    with TestClient(create_app(cfg)) as c:
        conf = c.get("/config")
        assert conf.status_code == 200
        assert conf.json()["llm"]["chat"]["api_key"] == "***"
        plugins = c.get("/plugins").json()
        assert "echo" in plugins["llm"] and "fake" in plugins["embedder"]


def test_web_ui_served_at_root():
    """内置浏览器聊天界面挂在首页(打开即用,无需命令行)。"""
    from fastapi.testclient import TestClient

    from services.api import create_app

    cfg = load_config(llm={"mode": "echo"}, embedder={"backend": "fake"},
                      vectordb={"mode": "memory"})
    with TestClient(create_app(cfg)) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        assert r.text.startswith("<!doctype html>") and "记忆助手" in r.text
        assert "./chat" in r.text and "./healthz" in r.text   # 前端打的是相对路由
        assert c.get("/ui").status_code == 200
