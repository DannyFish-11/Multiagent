"""M21 插件系统验收:注册表 + entry_points 发现 + 各工厂经注册表 + drop-in 三件套。"""

from __future__ import annotations

import sys
import types

import pytest

from core.errors import LayerError
from core.plugins import PluginRegistry, available, register


# ---------------------------------------------------------------- 注册表基本功

def test_register_get_available():
    reg = PluginRegistry()
    reg.register("llm", "foo")(lambda: "FOO")
    reg.add("llm", "bar", lambda: "BAR")
    assert reg.available("llm") == ["bar", "foo"]
    assert reg.get("llm", "foo")() == "FOO"
    assert reg.create("llm", "bar") == "BAR"


def test_unknown_plugin_raises_listing_available():
    reg = PluginRegistry()
    reg.add("llm", "known", object())
    with pytest.raises(LayerError) as ei:
        reg.get("llm", "nope")
    msg = str(ei.value)
    assert "nope" in msg and "known" in msg and "entry_points" in msg


def test_later_registration_overrides():
    reg = PluginRegistry()
    reg.add("embedder", "x", lambda: 1)
    reg.add("embedder", "x", lambda: 2)   # 用户覆写内置
    assert reg.create("embedder", "x") == 2


# ---------------------------------------------------------------- entry_points 发现(树外插件)

class _FakeEP:
    def __init__(self, name, obj):
        self.name = name
        self._obj = obj

    def load(self):
        return self._obj


def test_entry_point_discovery_kind_name(monkeypatch):
    reg = PluginRegistry()
    factory = lambda: "third-party"
    import importlib.metadata as md
    monkeypatch.setattr(md, "entry_points", lambda group=None: [_FakeEP("llm:thirdparty", factory)])
    # 首次访问触发发现
    assert "thirdparty" in reg.available("llm")
    assert reg.get("llm", "thirdparty")() == "third-party"


def test_entry_point_discovery_register_hook(monkeypatch):
    reg = PluginRegistry()

    def _hook(registry):
        registry.add("tool", "hooked", lambda: "H")
    import importlib.metadata as md
    monkeypatch.setattr(md, "entry_points", lambda group=None: [_FakeEP("mybundle", _hook)])
    assert reg.get("tool", "hooked")() == "H"


def test_bad_entry_point_does_not_break_discovery(monkeypatch):
    reg = PluginRegistry()

    class _BadEP:
        name = "llm:bad"
        def load(self):
            raise RuntimeError("boom")
    import importlib.metadata as md
    monkeypatch.setattr(md, "entry_points", lambda group=None: [_BadEP()])
    assert reg.available("llm") == []   # 加载失败被吞,不抛


# ---------------------------------------------------------------- 内置全部在场

def test_builtins_registered():
    import adapters.cloud, adapters.embedder, adapters.llm, core.experiment, core.factory  # noqa
    assert set(available("llm")) >= {"local", "api", "echo", "litellm"}
    assert set(available("embedder")) >= {"local", "remote", "jina_api", "fake"}
    assert set(available("memory")) >= {"qdrant", "simplemem"}
    assert set(available("cloud_provider")) >= {"local", "generic_rest", "ray"}
    assert set(available("task_source")) >= {"synthetic", "replay", "inspect"}


# ---------------------------------------------------------------- drop-in:自定义 LLM 插件端到端

def test_custom_llm_plugin_is_drop_in():
    """注册一个新 LLM 名字 → 仅凭 config.llm.mode 字符串即被 build_llm_client 解析。"""
    from adapters.llm import build_llm_client
    from core.config import load_config

    class MyLLM:
        async def chat(self, messages, **kw):
            return "custom!"

    register("llm", "myllm")(lambda config, role, ledger: MyLLM())
    cfg = load_config(llm={"mode": "myllm"})      # str 字段:第三方名可直接配
    client = build_llm_client(cfg)
    assert isinstance(client, MyLLM)


# ---------------------------------------------------------------- LiteLLM 适配器(fake litellm)

class _FakeLedger:
    def __init__(self):
        self.records = []

    def check_budget(self):
        pass

    def record(self, *a, **k):
        self.records.append((a, k))


async def test_litellm_adapter_with_fake_backend(monkeypatch):
    from core.config import load_config

    fake = types.ModuleType("litellm")

    async def _acompletion(**kw):
        assert kw["model"] == "anthropic/claude-3-5-sonnet"
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content="hi from litellm"))],
            usage=types.SimpleNamespace(prompt_tokens=5, completion_tokens=3))
    fake.acompletion = _acompletion
    monkeypatch.setitem(sys.modules, "litellm", fake)

    from adapters.llm_litellm import LiteLLMAdapter
    from core.schemas import Message

    cfg = load_config(llm={"chat": {"model": "anthropic/claude-3-5-sonnet", "api_key": "k"}})
    ledger = _FakeLedger()
    adapter = LiteLLMAdapter(cfg.llm.chat, ledger=ledger)
    out = await adapter.chat([Message(role="user", content="hi")])
    assert out == "hi from litellm"
    assert adapter.model == "anthropic/claude-3-5-sonnet"
    assert adapter.last_meta["usage"] == {"prompt_tokens": 5, "completion_tokens": 3}
    assert ledger.records and ledger.records[0][0][2] == 5   # prompt_tokens 入账


def test_litellm_via_registry_and_config(monkeypatch):
    """llm.mode=litellm 经 build_llm_client 解析到 LiteLLMAdapter(不需真装 litellm)。"""
    from adapters.llm import build_llm_client
    from adapters.llm_litellm import LiteLLMAdapter
    from core.config import load_config

    cfg = load_config(llm={"mode": "litellm", "chat": {"model": "gpt-4o"}})
    client = build_llm_client(cfg)
    assert isinstance(client, LiteLLMAdapter)


# ---------------------------------------------------------------- 云供应商工厂

def test_build_provider_local_and_errors():
    from adapters.cloud import LocalProcessProvider, build_provider
    from core.config import load_config

    assert isinstance(build_provider(load_config(cloud={"provider": "local"})), LocalProcessProvider)
    with pytest.raises(LayerError):
        build_provider(load_config(cloud={"provider": "none"}))
    with pytest.raises(LayerError):
        build_provider(load_config(cloud={"provider": "nonesuch"}))


def test_ray_provider_clear_error_without_dep():
    """cloud.provider=ray 但未装 ray → 明确 fail-fast 指明 extra(不静默)。"""
    import importlib.util

    from adapters.cloud import build_provider
    from core.config import load_config

    if importlib.util.find_spec("ray") is not None:
        pytest.skip("ray 已安装,缺依赖路径不适用(真 Ray 往返已手工验证)")
    with pytest.raises(LayerError) as ei:
        build_provider(load_config(cloud={"provider": "ray"}))
    assert "ray" in str(ei.value)


# ---------------------------------------------------------------- Inspect 任务源(samples 文件,免 inspect_ai)

def test_inspect_task_source_from_samples(tmp_path):
    import json

    from adapters.task_source_inspect import InspectTaskSource

    p = tmp_path / "samples.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [
        {"id": "s1", "input": "2+2?", "target": "4"},
        {"id": "s2", "input": "capital of France?", "target": "Paris"},
    ]), encoding="utf-8")
    tasks = InspectTaskSource({"type": "inspect", "samples": str(p)}, seed=0).stream()
    assert len(tasks) == 2
    assert {t.task_id for t in tasks} == {"s1", "s2"}
    t = next(t for t in tasks if t.task_id == "s1")
    assert t.payload["input"] == "2+2?" and t.truth["target"] == "4" and t.kind == "inspect"


def test_inspect_via_make_task_source(tmp_path):
    import json

    from core.experiment import make_task_source

    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps({"id": "x", "input": "q", "target": "a"}) + "\n", encoding="utf-8")
    src = make_task_source({"type": "inspect", "samples": str(p)}, 0)
    tasks = src.stream()
    assert len(tasks) == 1 and tasks[0].task_id == "x"


def test_inspect_task_ref_needs_dep_error_is_accurate():
    """task 引用但未装 inspect_ai → 明确报缺 inspect_ai(不误导);装了则用户模块错误照实报。"""
    import importlib.util

    from adapters.task_source_inspect import InspectTaskSource

    src = InspectTaskSource({"type": "inspect", "task": "nope.mod:foo"}, seed=0)
    if importlib.util.find_spec("inspect_ai") is None:
        with pytest.raises(LayerError) as ei:
            src.stream()
        assert "inspect_ai" in str(ei.value)           # 缺依赖时报缺依赖
    else:
        with pytest.raises(LayerError) as ei:
            src.stream()
        assert "加载 Inspect Task 失败" in str(ei.value)  # 装了则报用户模块错误,不误导


def test_discovery_reentrancy_guard(monkeypatch):
    """entry-point 回调在发现过程中回调进注册表(available)不死锁、不重复发现。"""
    reg = PluginRegistry()

    def _hook(registry):
        registry.available("llm")      # 发现进行中回调进来
        registry.add("tool", "reentrant", lambda: "R")

    import importlib.metadata as md
    monkeypatch.setattr(md, "entry_points", lambda group=None: [_FakeEP("bundle", _hook)])
    assert reg.get("tool", "reentrant")() == "R"
