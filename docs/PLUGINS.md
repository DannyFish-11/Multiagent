# 插件系统(M21)—— 随便拉一个,加上就能使

所有后端都是**按名字注册的插件**:LLM、嵌入、记忆、云供应商、任务源、工具、脚手架 profile。
业务代码只按名字取,不认识具体实现;加一个新后端 = 注册一个名字,别处零改动。

看当前有哪些:`make plugins`

```
llm             api, echo, litellm, local
embedder        fake, jina_api, local, remote
memory          qdrant, simplemem
cloud_provider  generic_rest, local, ray
task_source     inspect, replay, synthetic
profile         default, deepseek, gemma, glm, kimi, qwen
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
| `profile` | `()` | `HarnessProfile`(按模型脚手架,见下) |

---

## Harness Profile(M23)—— 让开源模型发挥真实水平

同一套为闭源旗舰调好的"脚手架"(系统提示 + 采样 + 工具循环处理),换到开源模型
(GLM/DeepSeek/Kimi/Qwen/本地 gemma…)上往往只发挥一半实力。**Harness Profile** 把每个
模型的调优参数打包成命名、可切换的 profile,随模型一起选。

选:`agent.profile`(config / `.env`)
- `auto`(默认):按当前 chat 模型名自动匹配内置 profile;**匹配不到回落 `default`(零脚手架,
  保持原生行为)**——闭源旗舰默认走 default,不动其表现。
- 具体名(如 `glm`)/ `none`。未注册名 → `doctor` 报错列出可用。

写自己的 profile(树内一行,或树外 `profile:xxx` entry point):
```python
from core.harness import HarnessProfile
from core.plugins import register

@register("profile", "mymodel")
def _p():
    return HarnessProfile(
        name="mymodel",
        match=("mymodel", "mymodel-chat"),   # auto:chat 模型名含任一子串即命中
        system_prompt="……模型专属指引(叠加在用户 system_prompt 之后)",
        sampling={"temperature": 0.3},        # 经 **kw 透传给 chat_tools
        tool_result_max_chars=4000,           # 工具结果回灌前截断(省 token/防塞爆上下文)
        max_tools_per_turn=None,              # 单轮工具批量上限(None=默认)
        max_steps=None,                       # 工具循环步数上限(None=用 config.loops)
    )
```
`system_prompt` 对 MemoryAgent 与 ToolAgent 都生效;其余(采样/截断/上限)在 ToolAgent
工具循环生效。全部是"加法/覆盖",空/None = 不改默认行为。

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
