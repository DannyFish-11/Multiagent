# 插件系统(M21)—— 随便拉一个,加上就能使

所有后端都是**按名字注册的插件**:LLM、嵌入、记忆、云供应商、任务源、工具。
业务代码只按名字取,不认识具体实现;加一个新后端 = 注册一个名字,别处零改动。

看当前有哪些:`make plugins`

```
llm             api, echo, litellm, local
embedder        fake, jina_api, local, remote
memory          qdrant, simplemem
cloud_provider  generic_rest, local, ray
task_source     inspect, replay, synthetic
```

选哪个只在 config / `.env` 写名字:`llm.mode` · `embedder.backend` · `memory.backend`
· `cloud.provider` · 实验 YAML 的 `task_source.type`。写了不存在的名字,报错会**列出所有可用名**。

---

## 现成 drop-in(已接好,装可选依赖即用)

| 想要 | 配置 | 装 |
|---|---|---|
| **100+ 家 LLM**(OpenAI/Claude/Gemini/Ollama/vLLM/DeepSeek…) | `llm.mode=litellm` + `llm.chat.model=anthropic/claude-3-5-sonnet` | `uv sync --extra litellm` |
| **大规模并发**(Ray 集群/多核调度) | `cloud.provider=ray` + `cloud.base_url=<集群地址或留空本机>` | `uv sync --extra ray` |
| **跑 benchmark 做实验**(Inspect-AI 评测/数据集) | 实验 YAML `task_source: {type: inspect, task: "pkg:my_task"}` 或 `{type: inspect, samples: data.jsonl}` | `uv sync --extra inspect` |

例:换成 Claude,`.env` 里两行:
```bash
MEMORY_AGENT_LLM__MODE=litellm
MEMORY_AGENT_LLM__CHAT__MODEL=anthropic/claude-3-5-sonnet
# key 走 litellm 认的环境变量(ANTHROPIC_API_KEY)或 MEMORY_AGENT_LLM__CHAT__API_KEY
```

---

## 树内写一个插件(20 行)

实现对应协议,注册一个名字即可。以自定义 LLM 为例:

```python
# adapters/llm_myllm.py
from core.plugins import register
from core.schemas import Message

class MyLLM:
    def __init__(self, role, ledger=None):
        self._model = role.model
    async def chat(self, messages: list[Message], **kw) -> str:
        ...                      # 调你的后端,返回字符串
        return "..."
    async def health(self) -> bool:
        return True

@register("llm", "myllm")        # ← 名字登记
def build_myllm(config, role, ledger):
    return MyLLM(config.llm.chat, ledger=ledger)
```

用:`llm.mode=myllm`。工厂签名按 kind:

| kind | 工厂签名 | 返回需实现 |
|---|---|---|
| `llm` | `(config, role, ledger)` | `async chat(messages,**kw)->str`(+ 可选 `health`) |
| `embedder` | `(settings, ledger)` | `async embed(inputs)->list[list[float]]` + `dim` |
| `memory` | `(config, embedder, llm)` | `async add/search/…`(MemoryStore 协议) |
| `cloud_provider` | `(config)` | `async create_vm/get_status/destroy_vm` |
| `task_source` | `(spec, seed)` | `stream()->list[Task]` |
| `tool` | `(config)` | 工具对象(经审批策略引擎治理) |

---

## 树外插件(pip 装个包就被发现,不改本仓库)

在你的第三方包 `pyproject.toml` 里声明 entry point group `memory_agent.plugins`:

```toml
[project.entry-points."memory_agent.plugins"]
# 形式①:名字 "kind:name" → 工厂
"llm:mycloud"      = "mypkg.llm:build_mycloud"
"embedder:myembed" = "mypkg.embed:build_myembed"

# 形式②:无冒号 → 一个 register(registry) 回调,自行注册多个
mybundle = "mypkg:register_all"
```

```python
# mypkg/__init__.py  —— 形式② 的回调
def register_all(registry):
    registry.add("llm", "a", build_a)
    registry.add("cloud_provider", "b", build_b)
```

`pip install mypkg`(或 `uv add mypkg`)后,`make plugins` 里就出现这些名字,
config 直接按名字选 —— **拉过来,加上就能使**。加载失败的第三方插件会被跳过并记日志,
不拖垮主流程。用户注册的同名插件覆盖内置(可覆写默认实现)。
