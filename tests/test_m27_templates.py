"""M27 验收:开箱即用场景模板(examples/*.yaml)结构合法、能装配、doctor 通过。

保证仓库里随附的多 agent 模板不会是坏的——加载 → build_swarm/build_supervisor 不抛 →
doctor 的结构项为 ok(llm base_url 的 fail 是预期,由用户经 .env 补全)。
"""

from __future__ import annotations

from core.config import PROJECT_ROOT, load_config

EX = PROJECT_ROOT / "examples"


class FakeMemory:
    async def search(self, query, k=5):
        return []

    async def add(self, inp, meta=None):
        return "id"


def _load(name, monkeypatch):
    monkeypatch.setenv("MEMORY_AGENT_CONFIG", str(EX / name))
    return load_config()


def test_swarm_template_valid(monkeypatch):
    from core.swarm import build_swarm

    cfg = _load("swarm-customer-service.yaml", monkeypatch)
    assert cfg.agent.autonomy == "swarm"
    sw = build_swarm(cfg, object(), FakeMemory())              # 不抛 = 结构合法
    assert set(sw._members) == {"intake", "tech", "finance", "summary"}
    assert sw._members["tech"].tool("recall") is not None      # 私有工具装上了


def test_supervisor_template_valid(monkeypatch):
    from core.supervisor import build_supervisor

    cfg = _load("supervisor-research-write.yaml", monkeypatch)
    assert cfg.agent.autonomy == "supervisor"
    sup = build_supervisor(cfg, object(), FakeMemory())
    assert {"delegate_to_researcher", "delegate_to_writer"} <= set(sup._tools)


def test_templates_pass_structural_doctor(monkeypatch):
    from core.doctor import run_doctor

    for name in ("swarm-customer-service.yaml", "supervisor-research-write.yaml"):
        cfg = _load(name, monkeypatch)
        checks = run_doctor(cfg)
        struct = [c for c in checks if "autonomy=" in c.title]
        assert struct and struct[0].level == "ok", f"{name} 结构项应为 ok"
