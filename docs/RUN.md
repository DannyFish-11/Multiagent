# 一键运行(双击即用)

三种方式,从"什么都不用装"到"从源码起",按你的情况选。

## 1. 下载即用(不用 clone、不用装 Python)⭐

从本仓库 **Releases** 页下载对应系统的可执行文件:

| 系统 | 文件 |
|---|---|
| Windows | `memory-agent-windows-x86_64.exe` |
| Linux | `memory-agent-linux-x86_64` |

- **Windows**:双击 `.exe`。
- **Linux**:`chmod +x memory-agent-linux-x86_64 && ./memory-agent-linux-x86_64`。

启动后会**自动打开浏览器**到聊天页(`http://127.0.0.1:8002`)。默认是 **demo 档**(零 key / 零 GPU,回显检索到的记忆,验证记忆闭环)。想用真实大模型:在可执行文件**同目录放一个 `.env`**(见下),重启即可。

> Release 二进制由 GitHub Actions 为每个 tag 自动构建,并在挂载前跑 `/healthz` 自检(产物先作为 draft release 生成,维护者确认后发布)。

## 2. 双击启动器(已 clone 仓库)

仓库根目录里:

- **Windows**:双击 `run.bat`
- **Linux / macOS**:双击 `run.sh`(或终端 `./run.sh`)

首次会自动装好运行环境(`uv`)+ 依赖,然后起服务并开浏览器。之后每次双击即用,不碰命令行。

## 3. 命令行(开发者)

```bash
uv run memory-agent start           # = 一键运行(零配置即 demo + 自动开浏览器)
uv run memory-agent start --demo    # 强制 demo 档
uv run memory-agent start --no-browser --port 9000
```

## 用真实大模型(可选)

放一个 `.env`(或用 `uv run memory-agent setup` 生成),填 OpenAI 兼容供应商:

```bash
MEMORY_AGENT_LLM__MODE=api
MEMORY_AGENT_LLM__CHAT__BASE_URL=https://api.deepseek.com/v1
MEMORY_AGENT_LLM__CHAT__MODEL=deepseek-chat
MEMORY_AGENT_LLM__CHAT__API_KEY=sk-...     # 只落 .env,绝不入库
MEMORY_AGENT_EMBEDDER__BACKEND=jina_api    # 语义检索(或 local 用本地模型)
MEMORY_AGENT_EMBEDDER__JINA_API_KEY=...
```

有 `.env` 或设了 `MEMORY_AGENT_LLM__*` 环境变量时,启动器**尊重你的配置、不回落 demo**。
多 agent(swarm / supervisor)开箱模板见 `examples/`。
