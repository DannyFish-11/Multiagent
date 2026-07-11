# 部署清单(交给任何人也能稳定跑)

一页照做即可。先 `memory-agent doctor` 体检,全绿再上。

## 0. 最小需求

- 一台常驻小机器(纯云端配置**无需 GPU**;1–2 核 / 1–2G 内存够跑 L3 + 本地向量库)
- 一把云端 LLM key(想要真实语义检索再加一把 Jina key);想先看效果用 `make demo` 零 key
- Python 3.12(装依赖用 uv);想用一键 Docker 则装 Docker

## 1. 装 + 配

```bash
git clone <repo> && cd multiagent
make install                 # uv sync
make setup                   # 首次运行向导:选 LLM/嵌入/预算 → 写 .env(密钥只落 .env)
memory-agent doctor          # 体检:有 ❌ 就按提示修,全绿再继续
```

## 2. 起

```bash
# 免 docker(向量库用本地文件):
make run-api                 # L3 API :8002;/healthz 分层报告依赖
# 或一键 Docker 全栈(API + Qdrant):
make quickstart
```

起好后**浏览器打开 http://localhost:8002 即可对话**(自带聊天界面);或 `make chat` 终端聊。
自检:`curl localhost:8002/healthz`(三层 ok)· `curl localhost:8002/config`(脱敏配置)· `curl localhost:8002/plugins`(已装插件)。

## 3. 稳定运行(生产)

- **进程守护**:docker `restart: unless-stopped`(compose 已配)或 systemd;`/healthz` 已内置健康检查,失败会分层指明哪层(不静默降级)。
- **成本硬闸**:`.env` 的 `MEMORY_AGENT_BUDGET__DAILY_USD` 是真实拦截,先设小值试水。
- **并发**:`concurrency.max_concurrent_llm_calls` 防限流雪崩;高并发把向量库换成服务端 Qdrant(`vectordb.mode=server`)。

## 4. 必须备份的三样(丢了很麻烦)

| 路径 | 是什么 | 丢了会怎样 |
|---|---|---|
| `./data/identity/` | Ed25519 **身份私钥**(终身不变) | agent "换了个人",签名/信任关系失效 |
| `./data/` | 记忆库 + 成本账本 | 记忆清零 |
| `./logs/audit.jsonl` | 不可篡改审计底账 | 追溯断档 |

挂到有快照/备份的盘;`.env`(密钥)单独安全保管,**不入库**(已 gitignore)。

## 5. 按需开的可选能力(默认全关)

| 能力 | 开 | 装 |
|---|---|---|
| 换任一 LLM(100+ 家) | `llm.mode=litellm` | `uv sync --extra litellm` |
| 大规模并发 | `cloud.provider=ray` | `uv sync --extra ray` |
| 跑 benchmark 实验 | `task_source: {type: inspect}` | `uv sync --extra inspect` |
| 可观测性看板 | `observability.enabled=true` | `uv sync --extra observability` + `make observability-up` |
| 本地嵌入(离线语义) | `embedder.backend=local` | `uv sync --extra local-embed`(重,需 GPU 更佳) |
| 上网 / Gmail / 支付 | 见 `config.yaml` 各段 | 供应商 key 由你填 |

## 6. 升级 / 排障

- 升级:`git pull && make install && memory-agent doctor && make test`。
- 排障:`docker compose logs memory-api`;`memory-agent doctor` 复检;任一依赖不可达时 `/healthz` 指明层号(`[L0/…]`/`[L1/…]`/`[L2/…]`)。
- 磁盘满:清 `./data/qdrant` 缓存或换盘;删旧快照 `experiments/snapshots/`。
