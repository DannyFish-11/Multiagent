# 快速上手(装了就能用)

三条路径,按手头资源选。**都不需要改代码,向导帮你写好 `.env`。**

---

## ① 一键 Docker 全栈(推荐)

需要:装好 Docker(Desktop 或 Engine)+ 一把云端 LLM key(可选再加一把 Jina key 做语义检索)。

```bash
make quickstart      # = 环境检查 → 首次运行向导 → docker compose up → 等健康检查
make chat            # 起来后,在终端里直接和它对话
```

起好后**浏览器打开 http://localhost:8002 就能直接聊**(自带聊天界面,显示命中的记忆),或用 `make chat` 在终端聊。

`quickstart` 会:
1. 检查 `docker` / `docker compose` 是否就绪;
2. 若无 `.env`,跑**首次运行向导**(`scripts/setup.py`)——问你 LLM 供应商(DeepSeek/OpenAI/…)、key、嵌入方式、日预算,把 `.env` 写好(密钥只落 `.env`,已 gitignore);
3. `docker compose up -d --build` 起 `qdrant` + `memory-api`;
4. 轮询 `/healthz` 直到三层依赖全绿。

停:`docker compose down`。改配置:`make setup` 重跑向导,再 `docker compose up -d`。

---

## ② 本地进程 · 云端 API(免 docker)

需要:一把云端 LLM key(+ 可选 Jina key)。向量库用进程内落盘文件,不起 Qdrant。

```bash
make install         # uv sync
make setup           # 向导:LLM/嵌入/预算 → 写 .env(运行方式选「本地进程」)
make run-api         # 起 L3 API(:8002);自动读 .env
make chat            # 另开终端对话
```

---

## ③ 零 key demo(先看效果)

不需要任何 key / GPU / docker。验证"存入→检索→复述"链路(哈希嵌入 + echo 回显,**不代表真实质量**)。

```bash
make demo            # 一键演示:存"我的猫叫 Benjamin"→ 问 → 打印命中
# 或想自己聊:
make setup           # 选「零 key demo」(或:uv run python scripts/setup.py --demo)
make run-api && make chat
```

---

## 我最少要准备什么?

| 想要 | 必需 | 不需要 |
|---|---|---|
| 真能对话+记忆 | 一台常驻小机器 + 1 把 LLM key（+ 建议 1 把 Jina key）+ 设个日预算 | GPU、自己下模型 |
| 先看效果 | 什么都不用（`make demo`） | key、GPU、docker |

- **数据/身份要备份**:`./data`(记忆 + 身份私钥,终身不变别弄丢)、`./logs`(审计底账)挂到有备份的盘。
- **成本硬闸**:`.env` 里 `MEMORY_AGENT_BUDGET__DAILY_USD` 是真实拦截,超了直接拒付。
- **纯云端可行**:`llm.mode=api` + `embedder.backend=jina_api` + `vectordb.mode=local`,全程零 GPU。
- 上网 / Gmail / 支付 / 云 VM / Langfuse 监管都是**可选、默认关**,需要时再逐个开(见 `README.md` / `config.yaml`)。

排障:`docker compose logs memory-api`;任一依赖不可达时 `/healthz` 会**分层指明**哪层(`[L0/…]`/`[L1/…]`/`[L2/…]`),不静默降级。
